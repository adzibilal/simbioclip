import os
import re
import json
import math
import time
import logging
from typing import List, Dict, Any, Optional
from app.pipeline.llm import llm_client
from app.models import Job, Clip, resolve_clip_duration

logger = logging.getLogger("simbioclip.pipeline.moments")

MAX_TRANSCRIPT_SEGMENTS = 500
CHUNK_SIZE = 50
CLIPS_PER_CHUNK = 2

VALID_HOOK_TYPES = [
    "claim", "story", "question", "insight", 
    "funny", "controversial", "emotional", "actionable"
]


def _truncate_segments(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sample transcript down to MAX_TRANSCRIPT_SEGMENTS evenly across timeline.

    Prevents the LLM prompt from blowing up on 2+ hour videos while still
    giving it a representative view of the full transcript.
    """
    if len(segments) <= MAX_TRANSCRIPT_SEGMENTS:
        return segments
    step = len(segments) / MAX_TRANSCRIPT_SEGMENTS
    result = []
    for i in range(MAX_TRANSCRIPT_SEGMENTS):
        idx = min(int(i * step), len(segments) - 1)
        result.append(segments[idx])
    logger.info(
        "Truncated %d transcript segments to %d (sampled evenly)",
        len(segments), len(result),
    )
    return result


def format_segments_for_llm(segments: List[Dict[str, Any]]) -> str:
    """Formats raw transcript segments into timestamped lines."""
    formatted_lines = []
    for seg in segments:
        start = seg.get("start", 0.0)
        end = seg.get("end", 0.0)
        text = seg.get("text", "").strip()
        formatted_lines.append(f"[{start:.2f} - {end:.2f}]: {text}")
    return "\n".join(formatted_lines)

_REASONING_TAG_RE = re.compile(
    r"<\s*(thinking|thought|reasoning|analysis|scratchpad|reflection)\b[\s\S]*?<\s*/\s*\1\s*>",
    re.IGNORECASE,
)


def _strip_reasoning(text: str) -> str:
    """Remove <thinking>…</thinking> style blocks the model sometimes emits.

    Handles both closed pairs (preferred) and unclosed leading tags by
    cutting from the opening tag to the first '[' that looks like JSON."""
    cleaned = _REASONING_TAG_RE.sub("", text)
    # If an opening reasoning tag is unclosed, drop everything up to the first JSON bracket.
    m = re.search(
        r"<\s*(?:thinking|thought|reasoning|analysis|scratchpad|reflection)\b",
        cleaned,
        re.IGNORECASE,
    )
    if m:
        bracket_idx = cleaned.find("[", m.end())
        if bracket_idx != -1:
            cleaned = cleaned[bracket_idx:]
    return cleaned


def _recover_objects(text: str) -> List[Dict[str, Any]]:
    """Extract all complete JSON objects from a possibly-truncated array.

    Used as a last resort when the LLM output was cut off before the closing `]`.
    Each fully-formed `{...}` block is parsed independently so we salvage as many
    clips as possible from the partial response.
    """
    results = []
    decoder = json.JSONDecoder()
    pos = 0
    while pos < len(text):
        pos = text.find("{", pos)
        if pos == -1:
            break
        try:
            obj, end = decoder.raw_decode(text, pos)
            if isinstance(obj, dict):
                results.append(obj)
            pos = end
        except json.JSONDecodeError:
            pos += 1
    return results


def parse_llm_json_response(raw_content: str) -> List[Dict[str, Any]]:
    """
    Cleans up and parses the JSON response from the LLM.
    Handles raw JSON, markdown blocks, leading/trailing trash,
    <thinking> reasoning tags, and truncated (partial) arrays.
    """
    cleaned = _strip_reasoning(raw_content).strip()

    # Strip markdown code fences if present (```json ... ```).
    cleaned = re.sub(r"^```(?:json|JSON)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned).strip()

    # 1. Prefer a top-level JSON array (complete or closeable).
    array_match = re.search(r"(\[\s*\{[\s\S]*\}\s*\])", cleaned)
    if array_match:
        try:
            data = json.loads(array_match.group(1))
            if isinstance(data, dict):
                data = [data]
            if isinstance(data, list):
                return data
        except Exception:
            pass

    # 2. Try a single JSON object wrapped in a list.
    obj_match = re.search(r"(\{[\s\S]*\})", cleaned)
    if obj_match:
        try:
            data = json.loads(f"[{obj_match.group(1)}]")
            if isinstance(data, list):
                return data
        except Exception:
            pass

    # 3. Partial-JSON recovery: extract every complete {…} block even from a
    #    truncated response.  This handles free-model output cutoffs gracefully.
    recovered = _recover_objects(cleaned)
    if recovered:
        logger.warning(
            "LLM response appears truncated (%d bytes). Recovered %d complete object(s) via partial-JSON parser.",
            len(raw_content), len(recovered),
        )
        return recovered

    logger.error(
        "Failed to parse JSON content (first 1500 chars): %s",
        raw_content[:1500],
    )
    raise ValueError("Invalid JSON returned from LLM")

def _lang_instruction(lang: str | None) -> str:
    if lang == "id":
        return "Write the titles, hooks, and reasons in Indonesian (Bahasa Indonesia) ONLY."
    if lang == "en":
        return "Write the titles, hooks, and reasons in English ONLY."
    if lang:
        return f"Write the titles, hooks, and reasons in the language corresponding to language code '{lang}'."
    return "Write the titles, hooks, and reasons in the same language as the transcript."


def _build_system_prompt(lang_instruction: str,
                         dur_min: int = 15, dur_max: int = 60,
                         content_type: Optional[str] = None) -> str:
    span = dur_max - dur_min
    sweet_lo = round(dur_min + span * 0.25)
    sweet_hi = round(dur_min + span * 0.75)
    duration_rule = (
        f"   - Target {dur_min}-{dur_max}s (sweet spot ~{sweet_lo}-{sweet_hi}s). "
        f"Every clip MUST land within {dur_min}-{dur_max}s.\n"
        f"   - Extend or trim a candidate to the nearest segment boundaries to fit the window. "
        f"If a moment genuinely cannot fit {dur_min}-{dur_max}s while keeping its hook + payoff, drop it.\n\n"
    )

    if content_type == "podcast":
        content_guide = (
            "   - Podcast / interview: funny exchanges, hot takes, relatable stories, "
            "surprising admissions, strong opinions, interesting tangents, disagreements, "
            "or emotional moments.\n"
            "   - CRITICAL: Cover DIFFERENT speakers and DIFFERENT topics. "
            "Do NOT pick multiple clips from the same 10-minute segment about the same subject.\n"
        )
    elif content_type == "tutorial":
        content_guide = (
            "   - Educational / how-to: a self-contained tip, insight, or explanation "
            "with clear before/after or problem/solution structure.\n"
        )
    elif content_type == "talking_head" or content_type == "vlog":
        content_guide = (
            "   - Talking head / vlog: personal stories, strong opinions, emotional beats, "
            "funny anecdotes, or surprising reveals.\n"
        )
    elif content_type == "game_stream":
        content_guide = (
            "   - Gaming: clutch plays, funny reactions, rage moments, unexpected outcomes, "
            "or entertaining commentary.\n"
        )
    else:
        content_guide = (
            "   - Podcast / interview / casual chat: funny exchanges, hot takes, relatable stories, surprising admissions, strong opinions, interesting tangents.\n"
            "   - Educational / how-to: a self-contained tip, insight, or explanation.\n"
            "   - Storytelling: a complete mini-story with a setup and a payoff.\n"
            "   - Comedy / entertainment / reactions: the funniest or most dramatic beats.\n\n"
        )

    return (
        "You are an expert short-form video editor for TikTok, Instagram Reels, and YouTube Shorts.\n"
        "You find the moments in a longer video that work best as standalone vertical clips (15-60s) — "
        "the parts that make someone stop scrolling and keep watching.\n\n"

        "# YOUR JOB\n"
        "From the timestamped transcript below, pick the BEST moments to cut into clips. "
        "Favour quality, but most real conversations contain several good moments — find them, don't be stingy. "
        "Match your judgement to the content:\n"
        f"{content_guide}"

        "# WHAT MAKES A GOOD CLIP\n"
        "1) HOOK — the first few seconds earn attention. A hook can be a bold or contrarian claim, a curiosity "
        "gap, a funny or relatable line, an intriguing question, a surprising fact, an emotional beat, or a "
        "strong opinion. It does NOT need to be dramatic. A genuinely interesting, funny, "
        "or surprising opening counts. Just avoid opening on pure filler when a "
        "stronger line is a segment or two away.\n"
        "2) PAYOFF — the clip resolves itself inside the window: a punchline lands, a point is made, a story "
        "finishes, a question gets answered.\n"
        "3) STANDALONE — someone who watches ONLY this clip understands it without the rest of the video.\n\n"

        "# DIVERSITY RULE\n"
        "Choose clips that feel DIFFERENT from each other — different speakers, different topics, "
        "different emotional tones, different types of hooks. Avoid returning 2+ clips from the same "
        "conversation segment about the same topic. Variety is more important than chasing a slightly "
        "higher score on a similar moment.\n\n"

        "# HOOK TYPES — classify each clip's hook_type\n"
        "   \"claim\"        — bold assertion, hot take, contrarian opinion\n"
        "   \"story\"        — personal anecdote, narrative, mini-story\n"
        "   \"question\"     — rhetorical or direct question that hooks curiosity\n"
        "   \"insight\"      — useful tip, explanation, mind-blowing fact\n"
        "   \"funny\"        — joke, punchline, humorous exchange\n"
        "   \"controversial\" — spicy take, debate, disagreement\n"
        "   \"emotional\"    — heartfelt, vulnerable, inspiring, angry\n"
        "   \"actionable\"   — practical advice, step-by-step, how-to\n\n"

        "# SCORE RUBRIC (1-10) — be honest, don't inflate\n"
        "   9-10  Outstanding. Specific + surprising + complete + clean boundaries.\n"
        "   7-8   Strong. Clear hook, clear payoff, fully self-contained.\n"
        "   5-6   Good. Solid and watchable. KEEP these.\n"
        "   3-4   Weak but usable. Include these if the content is conversational "
        "and you would otherwise return very few or no clips.\n"
        "   1-2   Unusable: filler, ads, or incomprehensible out of context. Drop.\n\n"

        "# OUTPUT\n"
        "Return ONLY a valid JSON array sorted by score descending. "
        "Return [] ONLY if the transcript is genuinely just ads, music, or silence.\n\n"
        "[\n"
        "  {\n"
        "    \"start\": 12.34,\n"
        "    \"end\": 47.21,\n"
        "    \"score\": 8,\n"
        "    \"hook\": \"the spoken opening line, near-verbatim from transcript, ≤100 chars\",\n"
        "    \"title\": \"scroll-stopping upload title, ≤70 chars\",\n"
        "    \"reason\": \"1 sentence: what makes this clip work (be specific)\",\n"
        "    \"hook_type\": \"claim | story | question | insight | funny | controversial | emotional | actionable\",\n"
        "  }\n"
        "]\n\n"

        f"# LANGUAGE\n{lang_instruction}\n"
        "Hook and title must read naturally in that language."
    )


def _build_user_prompt(formatted_transcript: str,
                       dur_min: int = 15, dur_max: int = 60,
                       max_clips: int = 5,
                       chunk_idx: int = 0, total_chunks: int = 1,
                       used_hook_types: Optional[List[str]] = None,
                       used_topic_hints: Optional[List[str]] = None) -> str:
    chunk_note = (
        f"You are analyzing part {chunk_idx + 1} of {total_chunks} of this video. "
        if total_chunks > 1 else ""
    )
    diversity_note = ""
    if used_hook_types:
        diversity_note = (
            f"\nIMPORTANT: Clips already selected from other parts of the video have "
            f"these hook_types: {', '.join(used_hook_types)}. "
            f"Pick clips with DIFFERENT hook_types for variety.\n"
        )
    if used_topic_hints:
        diversity_note += (
            f"Topics already covered: {', '.join(used_topic_hints[:5])}. "
            f"Avoid picking clips about the same topics.\n"
        )
    return (
        f"Analyze the transcript below. {chunk_note}\n\n"
        f"Find up to {max_clips} clip(s) that are engaging, funny, surprising, or informative. "
        f"Make sure each clip fits {dur_min}-{dur_max}s. "
        f"Score everything 5+; dip to 3-4 for conversational content if you'd otherwise return few or no clips.\n\n"
        f"{diversity_note}"
        "Return ONLY the JSON array — no explanation, no markdown fence.\n\n"
        "--- TRANSCRIPT ---\n"
        f"{formatted_transcript}"
    )


def _chunk_segments(segments: List[Dict[str, Any]], n_chunks: int) -> List[List[Dict[str, Any]]]:
    """Split segments into n_chunks roughly equal groups."""
    if n_chunks <= 1:
        return [segments]
    chunk_size = max(1, math.ceil(len(segments) / n_chunks))
    return [segments[i:i + chunk_size] for i in range(0, len(segments), chunk_size)]


def _speaker_chunk_segments(
    segments: List[Dict[str, Any]],
    diarized: Optional[List[Dict[str, Any]]],
    n_chunks: int,
) -> List[List[Dict[str, Any]]]:
    """Split segments by speaker changes instead of uniform time.

    Groups consecutive segments by speaker, then distributes these groups
    across n_chunks so each chunk has roughly equal speaker-group count.
    This keeps conversational turns intact within each LLM call.
    """
    if not diarized or n_chunks <= 1:
        return _chunk_segments(segments, n_chunks)

    speaker_map: Dict[str, List[Dict[str, Any]]] = {}
    for seg in segments:
        s = seg.get("start", 0.0)
        speaker = "Speaker A"
        for d in diarized:
            if d["start"] <= s < d["end"]:
                speaker = d.get("speaker", "Speaker A")
                break
        speaker_map.setdefault(speaker, []).append(seg)

    groups = list(speaker_map.values())
    if len(groups) <= 1:
        return _chunk_segments(segments, n_chunks)

    chunks: List[List[Dict[str, Any]]] = [[] for _ in range(n_chunks)]
    for i, group in enumerate(groups):
        chunks[i % n_chunks].extend(group)

    chunks = [c for c in chunks if c]
    logger.info(
        "Speaker-aware chunking: %d speaker groups → %d chunks",
        len(groups), len(chunks),
    )
    return chunks


def _deduplicate_clips(clips: List[Clip]) -> List[Clip]:
    """Remove clips with >50% temporal overlap (keep higher-scored one, already sorted desc)."""
    kept: List[Clip] = []
    for clip in clips:
        overlapping = False
        for k in kept:
            overlap = max(0.0, min(clip.end, k.end) - max(clip.start, k.start))
            shorter = min(clip.duration, k.duration)
            if shorter > 0 and overlap / shorter > 0.5:
                overlapping = True
                break
        if not overlapping:
            kept.append(clip)
    return kept


def _deduplicate_by_hook_type(clips: List[Clip]) -> List[Clip]:
    """Enforce hook_type diversity: cap each hook_type at 40% of total clips.

    Processes clips sorted by score descending. Keeps only the top-scored
    clips per type until the cap is reached.
    """
    if not clips:
        return clips
    max_per_type = max(1, math.ceil(len(clips) * 0.4))
    type_counts: Dict[str, int] = {}
    kept: List[Clip] = []
    for c in sorted(clips, key=lambda x: x.score, reverse=True):
        ht = c.hook_type or "unknown"
        if type_counts.get(ht, 0) < max_per_type:
            type_counts[ht] = type_counts.get(ht, 0) + 1
            kept.append(c)
    logger.info(
        "Hook-type dedup: %d → %d (max %d per type)",
        len(clips), len(kept), max_per_type,
    )
    return kept


def _re_rank_by_diversity(clips: List[Clip], target: int) -> List[Clip]:
    """MMR-inspired selection: pick clips that are both high-scored and diverse.

    Uses hook_type as the diversity signal. Selects iteratively: at each step
    pick the clip that maximizes score - 0.4 * similarity_to_already_selected.
    """
    if not clips or target >= len(clips):
        return clips

    selected: List[Clip] = []
    candidates: List[Clip] = sorted(clips, key=lambda c: c.score, reverse=True)

    # Seed with the highest-scored clip
    selected.append(candidates.pop(0))

    while len(selected) < target and candidates:
        best_idx = -1
        best_value = -float("inf")
        for i, c in enumerate(candidates):
            similarity = 0.0
            for s in selected:
                # Temporal overlap as similarity signal
                overlap = max(0.0, min(c.end, s.end) - max(c.start, s.start))
                shorter = min(c.duration, s.duration)
                temporal_sim = (overlap / shorter) if shorter > 0 else 0.0

                # Hook type match penalty
                type_match = 1.0 if (c.hook_type and c.hook_type == s.hook_type) else 0.0

                # Combine signals (weighted)
                similarity = max(similarity, temporal_sim * 0.6 + type_match * 0.4)

            mmr_score = c.score * 0.01 - 0.4 * similarity
            if mmr_score > best_value:
                best_value = mmr_score
                best_idx = i

        if best_idx >= 0:
            selected.append(candidates.pop(best_idx))
        else:
            break

    logger.info(
        "MMR diversity selection: %d → %d candidates → %d selected",
        len(clips), len(candidates) + len(selected), len(selected),
    )
    return selected


def _distinct_topic_hints(clip: Clip) -> List[str]:
    """Extract short topic hints from a clip's hook and reason text."""
    hints = []
    if clip.hook:
        tokens = clip.hook.lower().split()[:6]
        hints.extend(tokens)
    if clip.reason:
        tokens = clip.reason.lower().split()[:4]
        hints.extend(tokens)
    return hints


def _detect_chunk(
    job: Job,
    segments: List[Dict[str, Any]],
    max_clips: int,
    lang_instruction: str,
    dur_min: int,
    dur_max: int,
    chunk_idx: int = 0,
    total_chunks: int = 1,
    content_type: Optional[str] = None,
    used_hook_types: Optional[List[str]] = None,
    used_topic_hints: Optional[List[str]] = None,
) -> List[Clip]:
    """Call the LLM on one segment chunk and return parsed Clip objects."""
    formatted = format_segments_for_llm(segments)
    messages = [
        {"role": "system", "content": _build_system_prompt(lang_instruction, dur_min=dur_min, dur_max=dur_max, content_type=content_type)},
        {"role": "user", "content": _build_user_prompt(
            formatted, dur_min=dur_min, dur_max=dur_max,
            max_clips=max_clips, chunk_idx=chunk_idx, total_chunks=total_chunks,
            used_hook_types=used_hook_types,
            used_topic_hints=used_topic_hints,
        )},
    ]

    model_name = llm_client.routers[0].get("model", "?") if llm_client.routers else "?"
    logger.info(
        "[LLM] Request — chunk %d/%d, model: %s, segments: %d, want: %d clip(s)",
        chunk_idx + 1, total_chunks, model_name, len(segments), max_clips,
    )
    logger.debug("[LLM_PROMPT] %s", messages[1]["content"][:1500])

    t0 = time.monotonic()
    raw = llm_client.get_completion(messages=messages, temperature=0.7)
    elapsed = time.monotonic() - t0

    is_truncated = bool(raw.strip()) and not raw.rstrip().endswith("]")
    logger.info(
        "[LLM] Response — chunk %d/%d — %.1fs, %d bytes%s",
        chunk_idx + 1, total_chunks, elapsed, len(raw.encode("utf-8")),
        " ⚠ output truncated (partial recovery active)" if is_truncated else "",
    )
    if raw.strip():
        logger.debug("[LLM_RESPONSE] %s", raw[:3000])

    try:
        suffix = f"_chunk{chunk_idx + 1}" if total_chunks > 1 else ""
        with open(os.path.join(job.get_dir(), f"moments_raw_response{suffix}.txt"), "w", encoding="utf-8") as f:
            f.write(raw)
    except Exception as ex:
        logger.warning(f"Could not persist raw moments response: {ex}")

    try:
        parsed = parse_llm_json_response(raw)
    except ValueError as e:
        logger.warning(f"Chunk {chunk_idx + 1}/{total_chunks}: failed to parse LLM response: {e}")
        return []

    clips = _items_to_clips(parsed)
    return clips[:max_clips]


def detect_moments(
    job: Job,
    segments: List[Dict[str, Any]],
    diarized: Optional[List[Dict[str, Any]]] = None,
) -> List[Clip]:
    if not segments:
        logger.warning("No transcript segments provided. Cannot detect moments.")
        return []

    segments = _truncate_segments(segments)
    lang_instruction = _lang_instruction(job.lang)
    dur_min, dur_max = resolve_clip_duration(getattr(job, "clip_duration", "auto"))
    n_clips = job.max_clips
    content_type = getattr(job, "content_type", None)

    logger.info(
        "Target clip duration: %d-%ds (preset=%s), max_clips=%d, content_type=%s",
        dur_min, dur_max, getattr(job, "clip_duration", "auto"), n_clips, content_type,
    )

    if len(segments) <= CHUNK_SIZE:
        n_chunks = 1
    else:
        n_chunks = max(2, math.ceil(n_clips / CLIPS_PER_CHUNK))

    chunks = _speaker_chunk_segments(segments, diarized, n_chunks)
    logger.info(
        "Processing %d segments in %d chunk(s) (CHUNK_SIZE=%d, CLIPS_PER_CHUNK=%d, diarized=%s)",
        len(segments), len(chunks), CHUNK_SIZE, CLIPS_PER_CHUNK,
        bool(diarized),
    )

    all_clips: List[Clip] = []
    used_hook_types: List[str] = []
    used_topic_hints: List[str] = []

    for i, chunk in enumerate(chunks):
        clips_this_chunk = max(1, math.ceil(n_clips / len(chunks)) + 1)
        t0 = chunk[0].get("start", 0) if chunk else 0
        t1 = chunk[-1].get("end", 0) if chunk else 0
        logger.info(
            "Chunk %d/%d: %d segs, t=%.0fs-%.0fs, requesting up to %d clip(s)",
            i + 1, len(chunks), len(chunk), t0, t1, clips_this_chunk,
        )
        chunk_clips = _detect_chunk(
            job, chunk, clips_this_chunk, lang_instruction, dur_min, dur_max,
            chunk_idx=i, total_chunks=len(chunks),
            content_type=content_type,
            used_hook_types=used_hook_types if used_hook_types else None,
            used_topic_hints=used_topic_hints if used_topic_hints else None,
        )
        logger.info("Chunk %d/%d: got %d clip(s)", i + 1, len(chunks), len(chunk_clips))
        for c in chunk_clips:
            if c.hook_type:
                used_hook_types.append(c.hook_type)
            used_topic_hints.extend(_distinct_topic_hints(c))
        all_clips.extend(chunk_clips)

    all_clips.sort(key=lambda c: c.score, reverse=True)
    all_clips = _deduplicate_clips(all_clips)
    all_clips = _deduplicate_by_hook_type(all_clips)
    all_clips = _re_rank_by_diversity(all_clips, n_clips)
    selected = all_clips[:n_clips]
    logger.info(
        "Detected %d moments from %d candidates (capped at max %d). Hook types: %s",
        len(selected), len(all_clips), n_clips,
        [c.hook_type or "?" for c in selected],
    )
    return selected


def _items_to_clips(parsed_moments: List[Dict[str, Any]]) -> List[Clip]:
    """Validate raw LLM moment dicts and convert them into Clip models."""
    clips = []
    for i, item in enumerate(parsed_moments):
        try:
            start = float(item.get("start", 0.0))
            end = float(item.get("end", 0.0))
            score = int(item.get("score", 0))
            reason = item.get("reason", "").strip()
            hook = item.get("hook", "").strip()
            title = item.get("title", "").strip()
            hook_type = (item.get("hook_type") or "").strip() or None
            standalone_check = (item.get("standalone_check") or "").strip() or None

            raw_alts = item.get("title_alternatives") or []
            title_alternatives = []
            if isinstance(raw_alts, list):
                for alt in raw_alts[:3]:
                    a = str(alt).strip()
                    if a and a != title:
                        title_alternatives.append(a)

            raw_emp = item.get("emphasis") or []
            emphasis = []
            if isinstance(raw_emp, list):
                allowed_emoji = {"🤯", "😂", "🔥", "💀", "👀", "💡", "⚡", "🎯", "📈"}
                for e in raw_emp[:3]:
                    if not isinstance(e, dict):
                        continue
                    try:
                        t = float(e.get("t", -1))
                    except (TypeError, ValueError):
                        continue
                    emoji = str(e.get("emoji", "")).strip()
                    if t < start or t > end or not emoji:
                        continue
                    if emoji not in allowed_emoji:
                        # Keep only emoji from the allowed palette to avoid odd glyphs that may not render
                        continue
                    emphasis.append({"t": round(t, 2), "emoji": emoji})

            # Basic validation
            duration = end - start
            if duration <= 0:
                logger.warning(f"Clip {i} has invalid duration {duration:.1f}s. Skipping.")
                continue

            clip_id = f"clip_{i+1}"

            clip = Clip(
                id=clip_id,
                title=title or f"Viral Clip #{i+1}",
                hook=hook or "Check this out!",
                reason=reason or "High engagement moment",
                score=score,
                start=start,
                end=end,
                duration=round(duration, 2),
                hook_type=hook_type,
                standalone_check=standalone_check,
                title_alternatives=title_alternatives,
                emphasis=emphasis,
            )
            clips.append(clip)
        except Exception as e:
            logger.warning(f"Failed to parse clip item {item}: {e}")
            continue

    return clips
