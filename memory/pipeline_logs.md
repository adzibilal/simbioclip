---
name: pipeline-logs
description: Per-job pipeline.log written by worker, streamed to browser via SSE, displayed in Logs tab
metadata:
  type: project
---

Each pipeline run writes structured JSON lines to `{DATA_DIR}/jobs/{job_id}/pipeline.log` (truncated on retry).

**Why:** User needed real-time visibility into LLM calls and pipeline steps without SSH access.

**How to apply:**
- `app/pipeline/job_logger.py` — `JobFileHandler` attaches to `simbioclip` root logger
- `app/pipeline/orchestrator.py` — `attach_job_logger` / `detach_job_logger` in `process_video_job`
- `app/pipeline/moments.py` — `[LLM]` and `[LLM_RESPONSE]` prefixed entries for LLM calls
- `app/main.py` — `GET /api/jobs/{job_id}/logs/stream` SSE endpoint tails the file
- `app/templates/job_detail.html` — SSE client in JS, `logBuffer` persists across WebSocket refreshes
- `app/templates/partials/job_detail.html` — two-column layout: pipeline sidebar + Content/Logs tabs
- Both API and worker containers share the `clip_data` Docker volume at `/app/data`
