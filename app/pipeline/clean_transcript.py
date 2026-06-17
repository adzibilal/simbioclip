import logging
from typing import List, Dict, Any, Tuple, Optional

logger = logging.getLogger("simbioclip.pipeline.clean_transcript")

# Single-word fillers per language. Always merged with English fallback set.
FILLERS_BY_LANG = {
    "en": {"uh", "um", "er", "ah", "ehm", "mmm", "hmm", "uhm", "huh"},
    "id": {"eh", "em", "ehm", "emm", "anu", "aaa", "mmm", "hmm", "ya"},
    "es": {"eh", "este", "mmm"},
}

# Multi-word filler phrases. Matched against consecutive normalized words.
PHRASE_FILLERS_BY_LANG = {
    "en": ["you know", "i mean", "kind of", "sort of"],
    "id": ["ya kan", "gitu kan", "gitu ya", "apa ya", "apa namanya", "iya kan"],
    "es": [],
}

SILENCE_THRESHOLD = 0.8   # gap between word_end and next word_start that counts as silence
SILENCE_PADDING = 0.15    # keep this much audio around each silence so cuts feel natural


def _normalize(text: str) -> str:
    return text.lower().strip(".,!?;:\"'()[] ")


def clean_segments(
    segments: List[Dict[str, Any]],
    lang: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[List[float]]]:
    """
    Drops filler tokens from segment word lists and detects silence gaps.

    Returns:
      cleaned_segments: same structure as input, with fillers removed from .words and .text rebuilt.
      silence_ranges: [[s, e], ...] in absolute video time, padded inward by SILENCE_PADDING.
    """
    if not segments:
        return [], []

    base = (lang or "").lower()
    fillers = set(FILLERS_BY_LANG.get(base, set())) | FILLERS_BY_LANG["en"]
    phrase_fillers = list(PHRASE_FILLERS_BY_LANG.get(base, [])) + PHRASE_FILLERS_BY_LANG["en"]
    phrase_fillers_norm = [p.lower() for p in phrase_fillers]

    cleaned_segments: List[Dict[str, Any]] = []
    all_kept_words: List[Dict[str, Any]] = []
    removed_tokens = 0

    for seg in segments:
        words = seg.get("words") or []
        if not words:
            # No word-level data → keep segment unchanged. Filler removal needs word timestamps.
            cleaned_segments.append(seg)
            continue

        kept: List[Dict[str, Any]] = []
        i = 0
        while i < len(words):
            # Try multi-word phrase filler first (longest match wins)
            matched = False
            for phrase in sorted(phrase_fillers_norm, key=lambda p: -len(p.split())):
                tokens = phrase.split()
                if i + len(tokens) <= len(words):
                    joined = " ".join(_normalize(words[i + k].get("word", "")) for k in range(len(tokens)))
                    if joined == phrase:
                        removed_tokens += len(tokens)
                        i += len(tokens)
                        matched = True
                        break
            if matched:
                continue

            wt = _normalize(words[i].get("word", ""))
            if wt in fillers:
                removed_tokens += 1
                i += 1
                continue

            kept.append(words[i])
            i += 1

        if kept:
            rebuilt_text = " ".join((w.get("word") or "").strip() for w in kept).strip()
            cleaned_segments.append({
                **seg,
                "text": rebuilt_text or seg.get("text", ""),
                "words": kept,
            })
            all_kept_words.extend(kept)

    # Detect silence ranges in the cleaned timeline
    silence_ranges: List[List[float]] = []
    all_kept_words.sort(key=lambda w: float(w.get("start", 0.0)))
    for i in range(1, len(all_kept_words)):
        prev_end = float(all_kept_words[i - 1].get("end", 0.0))
        cur_start = float(all_kept_words[i].get("start", 0.0))
        gap = cur_start - prev_end
        if gap >= SILENCE_THRESHOLD:
            s = prev_end + SILENCE_PADDING
            e = cur_start - SILENCE_PADDING
            if e - s > 0.2:
                silence_ranges.append([round(s, 3), round(e, 3)])

    logger.info(
        f"Transcript cleanup: removed {removed_tokens} filler tokens; "
        f"detected {len(silence_ranges)} silence range(s)"
    )
    return cleaned_segments, silence_ranges
