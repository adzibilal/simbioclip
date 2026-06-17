import re
import json
import logging
from typing import List, Dict, Any, Optional
from app.pipeline.llm import llm_client

logger = logging.getLogger("simbioclip.pipeline.diarization")


def diarize_speakers(
    segments: List[Dict[str, Any]],
    lang: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if not segments:
        return []

    # Merge adjacent segments into larger chunks so the LLM gets far fewer
    # items to process.  This dramatically speeds up free-model inference.
    merged = _merge_for_diarization(segments)
    formatted = _format_segments(merged)

    lang_instruction = "Answer in the same language as the transcript."
    if lang == "id":
        lang_instruction = "Answer in Indonesian (Bahasa Indonesia)."

    system_prompt = (
        "You are a speaker diarization AI. Your task is to analyze a transcript "
        "and identify when different people are speaking.\n\n"
        "CRITICAL RULES:\n"
        "1. Identify speaker changes based on topic shifts, question-answer patterns, "
        "greetings, and conversational cues in the text.\n"
        "2. Return a JSON array where each segment has:\n"
        "   - 'start': float (seconds, must match the input timestamp precisely)\n"
        "   - 'end': float (seconds, must match the input timestamp precisely)\n"
        "   - 'speaker': string label like 'Speaker A', 'Speaker B', etc.\n"
        "3. DO NOT merge adjacent segments that have the same speaker — keep them separate.\n"
        "4. CRITICAL: When adjacent segments have the SAME speaker, you MUST still include "
        "both segments in the output (do not merge them).\n"
        "5. If the entire transcript seems to be a single speaker, label everything 'Speaker A'.\n"
        "6. Return ONLY valid JSON array. No preamble, no explanations.\n\n"
        "OUTPUT FORMAT:\n"
        "[\n"
        "  {\"start\": 0.0, \"end\": 2.5, \"speaker\": \"Speaker A\"},\n"
        "  {\"start\": 2.5, \"end\": 5.0, \"speaker\": \"Speaker B\"}\n"
        "]"
    )

    user_prompt = (
        f"Analyze who is speaking in each segment of this transcript.\n\n"
        f"--- TRANSCRIPT ---\n"
        f"{formatted}"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    logger.info(
        "Sending transcript to LLM for speaker diarization "
        f"({len(merged)} chunks merged from {len(segments)} segments)..."
    )

    try:
        raw = llm_client.get_completion(messages=messages, temperature=0.1)
        parsed = _parse_diarization(raw, merged)
        # Expand chunk-level labels back to original segments
        parsed = _expand_chunks(parsed, merged, segments)
        speaker_count = len(set(s["speaker"] for s in parsed))
        logger.info(f"Diarization complete: {speaker_count} speaker(s), {len(parsed)} segments")
        return parsed
    except Exception as e:
        logger.warning(f"LLM diarization failed, falling back to single speaker: {e}")
        return _fallback_single_speaker(segments)


def _merge_for_diarization(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge consecutive segments into larger chunks so fewer items go to the LLM.

    Each chunk keeps its *first* segment's start and its *last* segment's end.
    Text is joined with a space.
    """
    if not segments:
        return []

    MAX_CHUNKS = 40
    if len(segments) <= MAX_CHUNKS:
        return list(segments)

    chunk_size = max(1, len(segments) // MAX_CHUNKS)
    chunks = []
    for i in range(0, len(segments), chunk_size):
        group = segments[i : i + chunk_size]
        chunks.append({
            "start": group[0].get("start", 0.0),
            "end": group[-1].get("end", 0.0),
            "text": " ".join(s.get("text", "").strip() for s in group),
            "_group": group,
        })

    logger.info(
        f"Merged {len(segments)} segments into {len(chunks)} chunks "
        f"(~{chunk_size} segs/chunk) for LLM diarization"
    )
    return chunks


def _expand_chunks(
    parsed: List[Dict[str, Any]],
    chunks: List[Dict[str, Any]],
    original_segments: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Map chunk-level speaker labels from *parsed* back to *original_segments*."""
    # Build a lookup: chunk start → chunk
    chunk_map = {}
    for chunk in chunks:
        key = round(chunk.get("start", 0.0), 2)
        chunk_map[key] = chunk

    result = []
    for item in parsed:
        start = round(float(item.get("start", 0.0)), 2)
        speaker = str(item.get("speaker", "Speaker A")).strip()
        chunk = chunk_map.get(start)
        if chunk and "_group" in chunk:
            for seg in chunk["_group"]:
                result.append({
                    "start": seg.get("start", 0.0),
                    "end": seg.get("end", 0.0),
                    "speaker": speaker,
                    "text": seg.get("text", ""),
                })
        else:
            # Standalone segment (e.g. from a small transcript) — pass through
            speaker_match = next(
                (s for s in original_segments
                 if abs(s.get("start", 0.0) - start) < 0.01),
                None
            )
            result.append({
                "start": item.get("start", 0.0),
                "end": item.get("end", 0.0),
                "speaker": speaker,
                "text": speaker_match.get("text", "") if speaker_match else "",
            })

    return result


def _format_segments(segments: List[Dict[str, Any]]) -> str:
    lines = []
    for i, seg in enumerate(segments):
        start = seg.get("start", 0.0)
        end = seg.get("end", 0.0)
        text = seg.get("text", "").strip()
        lines.append(f"[{start:.2f} - {end:.2f}]: {text}")
    return "\n".join(lines)


def _parse_diarization(
    raw: str,
    original_segments: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    cleaned = raw.strip()
    match = re.search(r"(\[\s*\{[\s\S]*\}\s*\])", cleaned)
    if match:
        cleaned = match.group(1)
    else:
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        raise ValueError("Failed to parse LLM diarization output")

    if not isinstance(data, list):
        raise ValueError("Diarization output is not a list")

    original_map = {}
    for seg in original_segments:
        key = (seg.get("start", 0.0), seg.get("end", 0.0))
        original_map[key] = seg.get("text", "")

    result = []
    for item in data:
        start = float(item.get("start", 0.0))
        end = float(item.get("end", 0.0))
        speaker = str(item.get("speaker", "Speaker A")).strip()

        matched_text = ""
        for seg in original_segments:
            if abs(seg.get("start", 0.0) - start) < 0.01 and abs(seg.get("end", 0.0) - end) < 0.01:
                matched_text = seg.get("text", "")
                break

        result.append({
            "start": start,
            "end": end,
            "speaker": speaker,
            "text": matched_text,
        })

    return result


def _fallback_single_speaker(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "start": seg.get("start", 0.0),
            "end": seg.get("end", 0.0),
            "speaker": "Speaker A",
            "text": seg.get("text", ""),
        }
        for seg in segments
    ]


def get_active_speaker_at(
    diarized: List[Dict[str, Any]],
    timestamp: float,
) -> Optional[str]:
    for seg in diarized:
        if seg["start"] <= timestamp < seg["end"]:
            return seg.get("speaker")
    return None


def get_speaker_segments(
    diarized: List[Dict[str, Any]],
    clip_start: float,
    clip_end: float,
) -> List[Dict[str, Any]]:
    clipped = []
    for seg in diarized:
        if seg["end"] > clip_start and seg["start"] < clip_end:
            clipped.append({
                "start": max(seg["start"], clip_start),
                "end": min(seg["end"], clip_end),
                "speaker": seg.get("speaker", "Speaker A"),
                "text": seg.get("text", ""),
            })
    return clipped
