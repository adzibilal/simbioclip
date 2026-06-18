import os
import re
import json
import logging
from typing import List, Dict, Any
from app.pipeline.llm import llm_client
from app.models import Job, Clip, resolve_clip_duration

logger = logging.getLogger("simbioclip.pipeline.moments")

MAX_TRANSCRIPT_SEGMENTS = 300


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


def parse_llm_json_response(raw_content: str) -> List[Dict[str, Any]]:
    """
    Cleans up and parses the JSON response from the LLM.
    Handles raw JSON, markdown blocks, leading/trailing trash, and
    <thinking> reasoning tags emitted by some models.
    """
    cleaned = _strip_reasoning(raw_content).strip()

    # Strip markdown code fences if present (```json ... ```).
    cleaned = re.sub(r"^```(?:json|JSON)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned).strip()

    # Prefer a top-level JSON array; fall back to a single object wrapped in a list.
    array_match = re.search(r"(\[\s*\{[\s\S]*\}\s*\])", cleaned)
    if array_match:
        candidate = array_match.group(1)
    else:
        obj_match = re.search(r"(\{[\s\S]*\})", cleaned)
        candidate = f"[{obj_match.group(1)}]" if obj_match else cleaned

    try:
        data = json.loads(candidate)
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise ValueError("Parsed JSON is not a list")
        return data
    except Exception as e:
        logger.error(
            "Failed to parse JSON content (first 1500 chars): %s",
            raw_content[:1500],
        )
        raise ValueError(f"Invalid JSON returned from LLM: {e}")

def _lang_instruction(lang: str | None) -> str:
    if lang == "id":
        return "Write the titles, hooks, and reasons in Indonesian (Bahasa Indonesia) ONLY."
    if lang == "en":
        return "Write the titles, hooks, and reasons in English ONLY."
    if lang:
        return f"Write the titles, hooks, and reasons in the language corresponding to language code '{lang}'."
    return "Write the titles, hooks, and reasons in the same language as the transcript."


def _build_system_prompt(lang_instruction: str,
                         dur_min: int = 15, dur_max: int = 60) -> str:
    span = dur_max - dur_min
    sweet_lo = round(dur_min + span * 0.25)
    sweet_hi = round(dur_min + span * 0.75)
    duration_rule = (
        f"   - Target {dur_min}-{dur_max}s (sweet spot ~{sweet_lo}-{sweet_hi}s). "
        f"Every clip MUST land within {dur_min}-{dur_max}s.\n"
        f"   - Extend or trim a candidate to the nearest segment boundaries to fit the window. "
        f"If a moment genuinely cannot fit {dur_min}-{dur_max}s while keeping its hook + payoff, drop it.\n\n"
    )
    return (
        "You are an expert short-form video editor for TikTok, Instagram Reels, and YouTube Shorts.\n"
        "You find the moments in a longer video that work best as standalone vertical clips (15-60s) — "
        "the parts that make someone stop scrolling and keep watching.\n\n"

        "# YOUR JOB\n"
        "From the timestamped transcript below, pick the BEST moments to cut into clips. "
        "Favour quality, but most real conversations contain several good moments — find them, don't be stingy. "
        "Match your judgement to the content:\n"
        "   - Podcast / interview / casual chat: funny exchanges, hot takes, relatable stories, surprising admissions, strong opinions, interesting tangents.\n"
        "   - Educational / how-to: a self-contained tip, insight, or explanation.\n"
        "   - Storytelling: a complete mini-story with a setup and a payoff.\n"
        "   - Comedy / entertainment / reactions: the funniest or most dramatic beats.\n\n"

        "# WHAT MAKES A GOOD CLIP\n"
        "1) HOOK — the first few seconds earn attention. A hook can be a bold or contrarian claim, a curiosity "
        "gap, a funny or relatable line, an intriguing question, a surprising fact, an emotional beat, or a "
        "strong opinion. It does NOT need to be dramatic. A genuinely interesting, funny, "
        "or surprising opening counts. Just avoid opening on pure filler when a "
        "stronger line is a segment or two away.\n"
        "2) PAYOFF — the clip resolves itself inside the window: a punchline lands, a point is made, a story "
        "finishes, a question gets answered.\n"
        "3) STANDALONE — someone who watches ONLY this clip understands it without the rest of the video.\n\n"

        "# SCORE RUBRIC (1-10) — be honest, don't inflate\n"
        "   9-10  Outstanding. Specific + surprising + complete + clean boundaries.\n"
        "   7-8   Strong. Clear hook, clear payoff, fully self-contained.\n"
        "   5-6   Good. Solid and watchable. KEEP these.\n"
        "   3-4   Weak but usable. Include these if the content is conversational "
        "and you would otherwise return very few or no clips.\n"
        "   1-2   Unusable: filler, ads, or incomprehensible out of context. Drop.\n\n"

        "# OUTPUT\n"
        "Return ONLY a JSON array sorted by score descending. "
        "Return [] ONLY if the transcript is genuinely just ads, music, or silence.\n\n"
        "[\n"
        "  {\n"
        "    \"start\": 12.34,\n"
        "    \"end\": 47.21,\n"
        "    \"score\": 8,\n"
        "    \"hook\": \"the spoken opening line, near-verbatim from transcript, ≤100 chars\",\n"
        "    \"title\": \"scroll-stopping upload title, ≤70 chars\",\n"
        "    \"reason\": \"1 sentence: what makes this clip work (be specific)\",\n"
        "  }\n"
        "]\n\n"

        f"# LANGUAGE\n{lang_instruction}\n"
        "Hook and title must read naturally in that language."
    )


def _build_user_prompt(formatted_transcript: str,
                       dur_min: int = 15, dur_max: int = 60) -> str:
    return (
        "Analyze the transcript below.\n\n"
        "Scan for candidate moments (engaging, funny, surprising, or informative openings). "
        f"Make sure each clip fits {dur_min}-{dur_max}s. "
        "Score everything 5+; dip to 3-4 for conversational content if you'd otherwise return few or no clips.\n\n"
        "Return ONLY the JSON array — no explanation, no markdown fence.\n\n"
        "--- TRANSCRIPT ---\n"
        f"{formatted_transcript}"
    )


def _request_and_parse(job: Job, messages: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    raw_response = llm_client.get_completion(messages=messages, temperature=0.3)
    try:
        with open(os.path.join(job.get_dir(), "moments_raw_response.txt"), "w", encoding="utf-8") as f:
            f.write(raw_response)
    except Exception as ex:
        logger.warning(f"Could not persist raw moments response: {ex}")

    try:
        return parse_llm_json_response(raw_response)
    except ValueError as e:
        logger.warning(f"Failed to parse LLM response (falling back to empty moments): {e}")
        return []


def detect_moments(job: Job, segments: List[Dict[str, Any]]) -> List[Clip]:
    if not segments:
        logger.warning("No transcript segments provided. Cannot detect moments.")
        return []

    segments = _truncate_segments(segments)
    formatted_transcript = format_segments_for_llm(segments)
    lang_instruction = _lang_instruction(job.lang)
    dur_min, dur_max = resolve_clip_duration(getattr(job, "clip_duration", "auto"))
    logger.info(f"Target clip duration: {dur_min}-{dur_max}s (preset={getattr(job, 'clip_duration', 'auto')})")

    logger.info("Sending transcript to LLM for moment detection...")
    messages = [
        {"role": "system", "content": _build_system_prompt(lang_instruction, dur_min=dur_min, dur_max=dur_max)},
        {"role": "user", "content": _build_user_prompt(formatted_transcript, dur_min=dur_min, dur_max=dur_max)},
    ]
    parsed_moments = _request_and_parse(job, messages)
    clips = _items_to_clips(parsed_moments)

    clips.sort(key=lambda c: c.score, reverse=True)
    selected_clips = clips[:job.max_clips]
    logger.info(f"Detected {len(selected_clips)} moments (capped at max {job.max_clips})")

    return selected_clips


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
