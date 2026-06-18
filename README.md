# SimbioClip

Self-hosted AI video clipper — long-form video → vertical shorts with auto-captions. $0 recurring.

## Features

- Download video from URL (YouTube, etc. via yt-dlp) or upload a file
- Speech-to-text with timestamps (API router or local faster-whisper)
- Best moment detection via LLM (score + hook + title)
- Render vertical clips (9:16 / 1:1 / 4:5 / 16:9) with burned-in captions
- Multiple layouts: center-crop, split-cam, inset, face-track
- Selectable download resolution (360p–4K / Best)
- Custom caption styles: Bold Pop, Neon, Minimal, Karaoke, Podcast
- Web dashboard with HTMX live updates
- Async job queue (Redis + RQ)

## Architecture

```
                     ┌──────────────┐
                     │   Browser    │
                     │ (Dashboard)  │
                     └──────┬───────┘
                            │
                     ┌──────▼───────┐
                     │  FastAPI API │
                     │  (main.py)   │
                     └──┬───────┬───┘
                        │       │
               ┌────────▼─┐  ┌──▼──────────┐
               │   Redis  │  │   Worker    │
               │ (queue)  │  │ (RQ worker) │
               └──────────┘  └──┬──────────┘
                                │
        ┌───────────────────────┬┴──────────────────┐
        │                       │                   │
   ┌────▼────┐          ┌──────▼──────┐     ┌──────▼──────┐
   │ yt-dlp  │          │    LLM      │     │   FFmpeg    │
   │Download  │          │   Router    │     │   Render    │
   └─────────┘          └─────────────┘     └─────────────┘
```

- **API** — FastAPI: accepts jobs, serves dashboard, streams status via WebSocket
- **Worker** — RQ worker: runs the pipeline (download → transcribe → detect moments → render)
- **Redis** — Job queue + state storage
- **LLM Router** — External (OpenAI-compatible): moment detection via cheap/free LLMs

## Prerequisites

- Docker & Docker Compose
- VPS with min. 2GB RAM (without local Whisper) or 4GB+ (with local Whisper)
- OpenAI-compatible LLM endpoint (free options: Groq, OpenRouter, or your own LiteLLM)
- (Optional) YouTube cookies in Netscape format to bypass bot detection

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `API_TOKEN` | `super-secret-admin-token` | Auth token for dashboard & API |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection string |
| `LLM_ROUTERS` | JSON array | LLM router list for moment detection |
| `STT_MODE` | `local` | `router` (API) or `local` (faster-whisper CPU) |
| `STT_BASE_URL` | `""` | STT API base URL (router mode) |
| `STT_API_KEY` | `""` | STT API key (router mode) |
| `STT_MODEL` | `whisper-large-v3-turbo` | Primary STT model |
| `STT_MODEL_FALLBACK` | `whisper-large-v3` | Fallback STT model |
| `LLM_TIMEOUT` | `240` | Per-request LLM timeout (seconds) |
| `LLM_MAX_RETRIES` | `1` | Max LLM retries |
| `COOKIES_FILE` | `""` | Path to Netscape-format cookies.txt |

### LLM_ROUTERS Format

```json
[
  {"base_url": "https://api.groq.com/openai/v1", "api_key": "gsk_xxx", "model": "llama-3.3-70b-versatile"},
  {"base_url": "https://openrouter.ai/api/v1", "api_key": "sk-xxx", "model": "google/gemini-2.0-flash-001"}
]
```

## Deployment

### 1. Docker Compose (Local / VPS)

```bash
git clone https://github.com/adzibilal/simbioclip.git
cd simbioclip

cp .env.example .env
# Edit .env — fill in API_TOKEN, LLM_ROUTERS, etc.

./run.sh
# or manually:
docker compose build
docker compose up -d
```

Dashboard: `http://localhost:8000`

### 2. Coolify

1. Coolify → **Projects** → **+ New Project** → `simbioclip`
2. **+ New Resource** → **Docker Compose**
3. Source **GitHub** → select `adzibilal/simbioclip`, branch `main`
4. **Compose file path**: `docker-compose.coolify.yaml`
5. Fill in environment variables (`API_TOKEN`, `LLM_ROUTERS`, etc.) in the **Environment** tab
6. Set a domain for the **api** service in the **Domains** tab
7. **Deploy**

### 3. Manual (without Docker)

```bash
pip install -r requirements.txt

# Start Redis separately
redis-server &

# Worker
python3 -m app.worker &

# API
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/login` | - | Login (form `token`) |
| `GET` | `/` | Cookie | Dashboard |
| `GET` | `/healthz` | - | Health check |
| `POST` | `/preview` | Bearer | Preview video metadata |
| `POST` | `/jobs` | Bearer | Create a new job |
| `GET` | `/jobs/{id}` | Bearer | Job status + clips |
| `DELETE` | `/jobs/{id}` | Bearer | Delete a job |
| `POST` | `/jobs/{id}/retry` | Bearer | Retry a job |
| `POST` | `/jobs/{id}/retry/{step}` | Bearer | Retry from a specific step |
| `POST` | `/jobs/{id}/rerender` | Bearer | Re-render all clips |
| `POST` | `/jobs/{id}/clips/{cid}/rerender` | Bearer | Re-render a single clip |
| `POST` | `/jobs/{id}/clips/{cid}/trim` | Bearer | Trim a clip |
| `GET` | `/jobs/{id}/clips/{cid}` | Bearer | Download a clip |
| `POST` | `/cleanup` | Bearer | Delete old jobs |

### Example: Create a Job

```bash
curl -X POST https://clip.example.com/jobs \
  -H "Authorization: Bearer $API_TOKEN" \
  -F "source_url=https://youtube.com/watch?v=xxxx" \
  -F "max_clips=8" \
  -F "layout_mode=auto" \
  -F "aspect_ratio=9:16" \
  -F "download_resolution=1080p"
```

## Pipeline Steps

| Step | Description | Retryable |
|------|-------------|-----------|
| `download` | Download video via yt-dlp | Yes |
| `transcribe` | Speech-to-text + cleanup | Yes |
| `moments` | LLM scores clip-worthy moments | Yes |
| `classify` | Detect content type / layout | Yes |
| `diarize` | Identify speaker turns | Yes |
| `render` | Reframe + burn captions | Yes |

## Development

```bash
# Full rebuild without cache
./run.sh rebuild

# Tail logs
./run.sh logs

# Open a shell inside a container
./run.sh shell worker

# Restart services without rebuild
./run.sh restart
```
