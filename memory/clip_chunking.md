---
name: clip-chunking
description: Free-model output truncation fix — chunked LLM calls + partial JSON recovery in moments.py
metadata:
  type: project
---

Job 75989958 got only 1 clip for a 1-hour video (max=15) because `free-model` output was cut off at 341 bytes.

**Root cause:** No `max_tokens` set → free-model hits its default output limit mid-JSON array.

**Fixes in `app/pipeline/moments.py`:**
1. `_recover_objects()` — extracts complete `{...}` objects from truncated arrays via `json.JSONDecoder.raw_decode`
2. `parse_llm_json_response()` — calls `_recover_objects` as last resort before raising
3. `detect_moments()` — chunks 500 sampled segments into `ceil(max_clips / 2)` groups, calls LLM once per chunk requesting only 2 clips (keeps output ~300 bytes, under free-model limit)
4. `_deduplicate_clips()` — removes clips with >50% temporal overlap across chunks
5. Constants: `MAX_TRANSCRIPT_SEGMENTS=500`, `CHUNK_SIZE=50`, `CLIPS_PER_CHUNK=2`

**How to apply:** If clips are still too few, check `moments_raw_response_chunk*.txt` in the job dir for truncation signs. The fix is permanent but the free-model may still limit output if CLIPS_PER_CHUNK > 1 per chunk is too many.
