# SimbioClip

Self-hosted AI video clipper — long-form video → vertical shorts dengan caption otomatis. $0 recurring.

## Fitur

- Download video dari URL (YouTube, dll via yt-dlp) atau upload file
- Transkrip + timestamp (STT via API router atau faster-whisper lokal)
- Deteksi momen terbaik via LLM (skor + hook + judul)
- Render klip vertical (9:16 / 1:1 / 4:5 / 16:9) dengan caption ter-burn
- Multi layout: center-crop, split-cam, inset, face-track
- Pilih resolusi download (360p–4K / Best)
- Kustom caption style: Bold Pop, Neon, Minimal, Karaoke, Podcast
- Dashboard web + HTMX live updates
- Async job queue (Redis + RQ)

## Arsitektur

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

- **API** — FastAPI: menerima job, menyajikan dashboard, streaming status via WebSocket
- **Worker** — RQ worker: menjalankan pipeline (download → transkrip → deteksi momen → render)
- **Redis** — Antrian job + penyimpanan state
- **LLM Router** — Eksternal (OpenAI-compatible): deteksi momen via LLM murah/gratis

## Prerequisites

- Docker & Docker Compose
- VPS/min. 2GB RAM (tanpa Whisper lokal) atau 4GB+ (dengan Whisper lokal)
- LLM endpoint OpenAI-compatible (gratis: Groq, OpenRouter, atau LiteLLM sendiri)
- (Opsional) Cookies YouTube Netscape format untuk bypass bot detection

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `API_TOKEN` | `super-secret-admin-token` | Token auth dashboard & API |
| `REDIS_URL` | `redis://redis:6379/0` | Koneksi Redis |
| `LLM_ROUTERS` | JSON array | Daftar router LLM untuk moment detection |
| `STT_MODE` | `local` | `router` (API) atau `local` (faster-whisper CPU) |
| `STT_BASE_URL` | `""` | Base URL STT API (mode router) |
| `STT_API_KEY` | `""` | API key STT (mode router) |
| `STT_MODEL` | `whisper-large-v3-turbo` | Model STT utama |
| `STT_MODEL_FALLBACK` | `whisper-large-v3` | Fallback model STT |
| `LLM_TIMEOUT` | `240` | Timeout per request LLM (detik) |
| `LLM_MAX_RETRIES` | `1` | Maks retry LLM |
| `COOKIES_FILE` | `""` | Path ke cookies.txt Netscape format |

### Format LLM_ROUTERS

```json
[
  {"base_url": "https://api.groq.com/openai/v1", "api_key": "gsk_xxx", "model": "llama-3.3-70b-versatile"},
  {"base_url": "https://openrouter.ai/api/v1", "api_key": "sk-xxx", "model": "google/gemini-2.0-flash-001"}
]
```

## Deployment

### 1. Docker Compose (Local / VPS manual)

```bash
git clone https://github.com/adzibilal/simbioclip.git
cd simbioclip

cp .env.example .env
# Edit .env — isi API_TOKEN, LLM_ROUTERS, dll

./run.sh
# atau manual:
docker compose build
docker compose up -d
```

Dashboard: `http://localhost:8000`

### 2. Coolify

1. Coolify → **Projects** → **+ New Project** → `simbioclip`
2. **+ New Resource** → **Docker Compose**
3. Sumber **GitHub** → pilih repo `adzibilal/simbioclip`, branch `main`
4. **Compose file path**: `docker-compose.coolify.yaml`
5. Isi environment variables (`API_TOKEN`, `LLM_ROUTERS`, dll) di tab **Environment**
6. Set domain untuk service **api** di tab **Domains**
7. **Deploy**

### 3. Manual ( tanpa Docker )

```bash
pip install -r requirements.txt

# Jalankan Redis terpisah
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
| `POST` | `/jobs` | Bearer | Buat job baru |
| `GET` | `/jobs/{id}` | Bearer | Status job + clips |
| `DELETE` | `/jobs/{id}` | Bearer | Hapus job |
| `POST` | `/jobs/{id}/retry` | Bearer | Retry job |
| `POST` | `/jobs/{id}/retry/{step}` | Bearer | Retry dari step tertentu |
| `POST` | `/jobs/{id}/rerender` | Bearer | Re-render semua clips |
| `POST` | `/jobs/{id}/clips/{cid}/rerender` | Bearer | Re-render satu clip |
| `POST` | `/jobs/{id}/clips/{cid}/trim` | Bearer | Trim clip |
| `GET` | `/jobs/{id}/clips/{cid}` | Bearer | Download clip |
| `POST` | `/cleanup` | Bearer | Hapus job lama |

### Contoh create job

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
| `download` | Download video via yt-dlp | Ya |
| `transcribe` | Speech-to-text + cleanup | Ya |
| `moments` | LLM scores clip-worthy moments | Ya |
| `classify` | Deteksi content type / layout | Ya |
| `diarize` | Identifikasi speaker | Ya |
| `render` | Reframe + burn captions | Ya |

## Development

```bash
# Build tanpa cache
./run.sh rebuild

# Logs
./run.sh logs

# Shell ke container
./run.sh shell worker

# Restart service tanpa rebuild
./run.sh restart
```
