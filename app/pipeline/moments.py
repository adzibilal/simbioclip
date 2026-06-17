import os
import re
import json
import logging
from typing import List, Dict, Any
from app.pipeline.llm import llm_client
from app.models import Job, Clip, resolve_clip_duration

logger = logging.getLogger("simbioclip.pipeline.moments")

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


def _build_system_prompt(lang_instruction: str, lenient: bool = False,
                         dur_min: int = 15, dur_max: int = 60) -> str:
    """Build the moments system prompt.

    `lenient=True` is used for a fallback pass when the strict pass returned no
    clips — it relaxes the hook bar and the score threshold so ordinary (but
    still watchable) conversational content isn't rejected wholesale.

    `dur_min`/`dur_max` set the target clip-length window the user chose.
    """
    span = dur_max - dur_min
    sweet_lo = round(dur_min + span * 0.25)
    sweet_hi = round(dur_min + span * 0.75)
    duration_rule = (
        f"   - Target {dur_min}-{dur_max} seconds (sweet spot ~{sweet_lo}-{sweet_hi}s). "
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
        "strong opinion. It does NOT need to be a dramatic 'guru' statement — a genuinely interesting, funny, "
        "or surprising opening counts. Just avoid opening on pure filler (\"so basically\", \"um, yeah\") when a "
        "stronger line is a segment or two away.\n"
        "2) PAYOFF — the clip resolves itself inside the window: a punchline lands, a point is made, a story "
        "finishes, a question gets answered. Avoid \"I'll tell you in a sec\" with the answer outside the clip.\n"
        "3) STANDALONE — someone who watches ONLY this clip understands it without the rest of the video.\n\n"

        "# BOUNDARIES\n"
        "Prefer clips whose first line sets up context and whose last line delivers the payoff. If a candidate "
        "starts mid-thought or ends on an unresolved setup, FIRST try moving the start/end to nearby segment "
        "boundaries to fix it; only drop it if it still doesn't work. Always start and end exactly on transcript "
        "segment boundaries — never split a sentence.\n\n"

        "# AVOID (do not output these)\n"
        "   - Pure intros/outros, ads, sponsor reads, \"subscribe and like\", greetings\n"
        "   - Clips that only make sense with earlier context a viewer can't see\n"
        "   - Generic filler with nothing interesting, funny, or informative\n\n"

        "# DURATION\n"
        + duration_rule +

        "# OUTPUT\n"
        "Return ONLY a JSON array. NO markdown fence. NO preamble. NO trailing prose.\n"
        "Sort by score descending. "
        "Return [] ONLY if the transcript contains no usable spoken content at all (pure music, ads, or silence).\n\n"
        "[\n"
        "  {\n"
        "    \"start\": 12.34,                           // seconds, MUST equal a transcript segment start\n"
        "    \"end\": 47.21,                             // seconds, MUST equal a transcript segment end\n"
        "    \"score\": 8,                               // 1-10, see rubric\n"
        "    \"hook_type\": \"contrarian|curiosity|story|claim|question|interrupt|funny|relatable\",\n"
        "    \"hook\": \"the spoken opening line, near-verbatim from transcript, ≤100 chars\",\n"
        "    \"title\": \"scroll-stopping upload title, ≤70 chars (BEST option)\",\n"
        "    \"title_alternatives\": [\"alt 1\", \"alt 2\"],  // OPTIONAL 0-2 additional title variants for A/B testing\n"
        "    \"reason\": \"1 sentence: what makes this clip work (be specific)\",\n"
        "    \"standalone_check\": \"1 sentence proving the first line sets context AND the last line delivers payoff\",\n"
        "    \"emphasis\": [{\"t\": 13.5, \"emoji\": \"🤯\"}],   // OPTIONAL 0-3 emoji at moments of surprise/punchline (timestamp in seconds, within [start, end])\n"
        "  }\n"
        "]\n\n"
        "EMPHASIS RULES (only if you add them):\n"
        "   - 0-3 emoji per clip — fewer is better. ONLY at genuine surprise / punchline / number-drop moments.\n"
        "   - Allowed emoji: 🤯 😂 🔥 💀 👀 💡 ⚡ 🎯 📈 (pick the most fitting)\n"
        "   - Each emphasis.t MUST be inside [start, end] and aligned with a transcript word\n"
        "   - NO emoji for generic statements\n\n"
        "TITLE ALTERNATIVES (only if you add them):\n"
        "   - Each alternative must be a DIFFERENT angle (e.g., shocking number, contrarian, story, direct question)\n"
        "   - All must accurately reflect the clip — no clickbait that the content doesn't deliver\n\n"

        "# SCORE RUBRIC (1-10) — be honest, don't inflate\n"
        "   9-10  Outstanding. Specific + surprising + complete + clean boundaries. Could be a top-comment quote.\n"
        "   7-8   Strong. Clear hook, clear payoff, fully self-contained.\n"
        "   5-6   Good. Solid and watchable, self-contained even if the hook is mild or the payoff is soft. KEEP these.\n"
        "   3-4   Weak but usable as a last resort.\n"
        "   1-2   Unusable: filler, ads, or incomprehensible out of context. Drop.\n"
        + (
            "Include every moment scoring 3+. This is a fallback pass: a stricter pass found nothing, so be "
            "permissive and surface the most interesting / funny / informative moments you can.\n\n"
            if lenient else
            "Include every moment scoring 5+. Only dip down to 3-4 if you would otherwise have very few clips.\n\n"
        )

        + f"# LANGUAGE\n{lang_instruction}\n"
        "Hook and title must read naturally in that language — do not translate awkwardly. "
        "If the transcript language differs from the requested output language, still write hook/title/reason/standalone_check in the requested language."
    )


def _build_user_prompt(formatted_transcript: str, lenient: bool = False,
                       dur_min: int = 15, dur_max: int = 60) -> str:
    if lenient:
        intro = (
            "A first, stricter pass found NO clips. This is real spoken content, so usable moments DO exist. "
            f"Be more permissive now: pick the 3-5 most interesting, funny, surprising, or informative segments "
            f"that work as standalone clips of {dur_min}-{dur_max}s. A clear, engaging, self-contained moment is "
            "enough — the hook does not have to be extraordinary. Only return [] if the transcript is genuinely "
            "just ads, music, or silence.\n\n"
        )
    else:
        intro = (
            "Analyze the transcript below.\n\n"
            "Before writing the JSON, silently do this:\n"
            "  1. Scan for candidate moments (engaging, funny, surprising, or informative openings).\n"
            "  2. For each candidate, find the earliest segment boundary where the hook starts "
            "and the latest segment boundary where the payoff lands.\n"
            f"  3. Make sure the span fits {dur_min}-{dur_max}s; adjust to nearby boundaries to fit. "
            "Prefer candidates that read as standalone.\n"
            "  4. Score what you find. Keep everything 5+; dip to 3-4 only if you'd otherwise have very few clips.\n\n"
        )
    return (
        intro
        + "Then output the JSON array ONLY — no explanation, no markdown fence.\n\n"
        "--- TRANSCRIPT ---\n"
        f"{formatted_transcript}"
    )


def _request_and_parse(job: Job, messages: List[Dict[str, str]], tag: str) -> List[Dict[str, Any]]:
    """Call the LLM, persist the raw reply, and parse it into a list of moments.

    On a JSON parse failure, retries once with a strict JSON-only follow-up.
    `tag` distinguishes the persisted artifact files (e.g. "" or "_lenient").
    """
    raw_response = llm_client.get_completion(messages=messages, temperature=0.3)
    try:
        with open(os.path.join(job.get_dir(), f"moments_raw_response{tag}.txt"), "w", encoding="utf-8") as f:
            f.write(raw_response)
    except Exception as ex:
        logger.warning(f"Could not persist raw moments response: {ex}")

    logger.info(f"Received moments response{tag or ' (strict)'}. Parsing...")
    try:
        return parse_llm_json_response(raw_response)
    except ValueError as first_err:
        logger.warning(f"First parse failed ({first_err}); retrying with strict JSON-only prompt")
        retry_messages = messages + [
            {"role": "assistant", "content": raw_response},
            {
                "role": "user",
                "content": (
                    "Your previous reply could not be parsed as JSON. "
                    "Respond again with ONLY the JSON array. "
                    "No <thinking> tags, no commentary, no markdown fences."
                ),
            },
        ]
        raw_response = llm_client.get_completion(messages=retry_messages, temperature=0.1)
        try:
            with open(os.path.join(job.get_dir(), f"moments_raw_response{tag}_retry.txt"), "w", encoding="utf-8") as f:
                f.write(raw_response)
        except Exception:
            pass
        return parse_llm_json_response(raw_response)


def detect_moments(job: Job, segments: List[Dict[str, Any]]) -> List[Clip]:
    """
    Queries LLM with the formatted transcript, parses the moments,
    filters/validates them, and returns a list of Clip models.

    Runs a strict pass first; if it yields no clips, retries once with a more
    lenient prompt so ordinary conversational content (podcasts, casual chats)
    doesn't fail the whole pipeline with "no engaging moments".
    """
    if not segments:
        logger.warning("No transcript segments provided. Cannot detect moments.")
        return []

    formatted_transcript = format_segments_for_llm(segments)
    lang_instruction = _lang_instruction(job.lang)
    dur_min, dur_max = resolve_clip_duration(getattr(job, "clip_duration", "auto"))
    logger.info(f"Target clip duration: {dur_min}-{dur_max}s (preset={getattr(job, 'clip_duration', 'auto')})")

    logger.info("Sending transcript to LLM client (strict pass)...")
    messages = [
        {"role": "system", "content": _build_system_prompt(lang_instruction, lenient=False, dur_min=dur_min, dur_max=dur_max)},
        {"role": "user", "content": _build_user_prompt(formatted_transcript, lenient=False, dur_min=dur_min, dur_max=dur_max)},
    ]
    parsed_moments = _request_and_parse(job, messages, tag="")

    clips = _items_to_clips(parsed_moments)

    if not clips:
        logger.warning("Strict pass found 0 clips; retrying with lenient prompt.")
        lenient_messages = [
            {"role": "system", "content": _build_system_prompt(lang_instruction, lenient=True, dur_min=dur_min, dur_max=dur_max)},
            {"role": "user", "content": _build_user_prompt(formatted_transcript, lenient=True, dur_min=dur_min, dur_max=dur_max)},
        ]
        parsed_moments = _request_and_parse(job, lenient_messages, tag="_lenient")
        clips = _items_to_clips(parsed_moments)

    # Sort clips by score descending
    clips.sort(key=lambda c: c.score, reverse=True)

    # Limit to max_clips
    selected_clips = clips[:job.max_clips]
    logger.info(f"Successfully detected & validated {len(selected_clips)} moments (capped at max {job.max_clips})")

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
