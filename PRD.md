# PRD — SimbioClip (MVP)

**Self-hosted AI video clipper untuk pemakaian pribadi — long-form → vertical shorts, $0 recurring.**

> Versi: PRD MVP v1.0 · Scope: penggunaan pribadi (single user) · Deploy: VPS via Coolify · Otak AI di-offload ke router LLM gratis sendiri.

---

## 1. Ringkasan

Tool clipping berbayar terbatas & ber-watermark di tier gratis. Semua komponennya sebenarnya tersedia gratis: `yt-dlp` (download), transkrip (STT gratis / `faster-whisper`), pemilihan momen (LLM via router gratis), `FFmpeg` (render). SimbioClip merangkainya jadi satu pipeline otomatis yang di-self-host.

**Problem → Solution → Result**
- **Problem:** bikin shorts dari video panjang itu manual & makan waktu; tool otomatis berbayar/terbatas.
- **Solution:** pipeline self-host: URL/file → klip 9:16 ber-caption otomatis, beban AI didorong ke router gratis.
- **Result:** dipakai sendiri dengan biaya hanya VPS yang sudah ada; $0 recurring.

---

## 2. Scope MVP

### Masuk MVP
1. Input via URL (yt-dlp) **atau** upload file.
2. Transkrip + timestamp.
3. Deteksi 5–15 momen via LLM, dengan skor & hook.
4. Render klip 9:16 (center-crop) + subtitle ter-burn.
5. Async job (antri 1 per 1) supaya VPS kecil tidak overload.
6. Rotasi/fallback antar 9 router (tahan rate limit, tetap gratis).
7. Dashboard minimal: submit job + lihat status + download.

### Di luar MVP (sengaja ditunda)
- Auto-post ke TikTok/Reels/Shorts.
- Multi-user / auth kompleks (MVP cukup 1 token).
- Speaker/face tracking 9:16 (MVP center-crop dulu).
- Object storage, B-roll AI, dubbing, editor visual.

---

## 3. User Stories (inti)

| # | Saya ingin | Supaya |
|---|-----------|--------|
| US-1 | tempel URL atau upload file | tidak download/siapkan manual |
| US-2 | sistem otomatis pilih momen + skor + hook | tidak nonton video penuh & tahu prioritas |
| US-3 | klip langsung 9:16 + caption | siap upload tanpa edit lagi |
| US-4 | proses jalan di background | bisa antri beberapa video |
| US-5 | lihat status & download hasil | tahu progres & ambil output |
| US-6 | router ganti otomatis saat limit | proses jalan terus & tetap $0 |

---

## 4. Pipeline (Functional Requirements)

Dijalankan sebagai background job; tiap tahap update status.

**FR-1 Ingest** — terima `source_url` atau upload; validasi durasi/ukuran (cap konfigurable, mis. 3 jam / 2 GB).

**FR-2 Download** — `yt-dlp`, cap 1080p, simpan ke `data/jobs/{job_id}/source.*`.

**FR-3 Transcribe**
- Mode A (default jika ada router STT): offload ke Whisper gratis (mis. Groq) → segmen `{start,end,text}`.
- Mode B (fallback): `faster-whisper` lokal, model `small`/`base` (hemat RAM).

**FR-4 Moment Detection (LLM)** — kirim transkrip ber-timestamp (teks saja) ke router LLM → JSON momen (§6) → validasi → sort by `score` → ambil top-N. Wajib lewat client router (load-balance + fallback, §5.3).

**FR-5 Render** — per momen: `FFmpeg` potong `start`→`end`, reframe 9:16 (center-crop), generate `.srt`/ASS dari segmen di rentang klip, burn caption → `data/jobs/{job_id}/clips/clip_{n}.mp4`.

**FR-6 Output & Status** — status job + daftar klip + metadata (skor, hook, durasi); dashboard sediakan link download.

**Status flow:** `queued → downloading → transcribing → finding_moments → rendering → done | failed`

---

## 5. Arsitektur Teknis

### 5.1 Pembagian beban (kunci VPS kecil)
```
VPS (ringan):
  API (FastAPI)     → terima job, lapor status
  Worker (RQ)       → yt-dlp + FFmpeg + orkestrasi
  Redis             → queue + state
  (faster-whisper)  → hanya jika Mode B

Router gratis (eksternal, milik sendiri):
  LLM moment detection   (9 router, load-balanced)
  STT Whisper (opsional) → offload transkrip (Mode A)
```
Prinsip: **VPS hanya kerja mekanis (download + potong)**; STT & LLM didorong ke router gratis.

### 5.2 Stack
- API: **FastAPI** · Queue: **Redis + RQ** (ringan untuk single-node).
- Media: `yt-dlp`, `FFmpeg`, `faster-whisper` (Mode B).
- LLM: OpenAI-compatible SDK → endpoint router (`ai.adzibilal.my.id/v1` / LiteLLM).
- Frontend MVP: halaman minimal yang diserve FastAPI (HTML/HTMX).
- Storage: volume lokal Coolify.

### 5.3 Strategi router ($0)
- Jika endpoint sudah LiteLLM: daftarkan 9 deployment, set `routing_strategy` + `fallbacks`; app cukup panggil **satu** base URL.
- Jika belum: client dengan daftar 9 base_url/key, round-robin + retry on `429/5xx` → router berikutnya.
- Selalu minta output **JSON-only, no preamble**.

### 5.4 Struktur repo
```
simbioclip/
├── docker-compose.yaml
├── Dockerfile
├── requirements.txt
├── .env.example
└── app/
    ├── main.py            # FastAPI: POST /jobs, GET /jobs/{id}, dashboard
    ├── worker.py          # entry RQ worker
    ├── config.py          # baca ENV
    ├── models.py          # skema Job & Clip
    └── pipeline/
        ├── download.py    # yt-dlp
        ├── transcribe.py  # Mode A / Mode B
        ├── moments.py     # prompt + parse JSON
        ├── llm.py         # router client (load-balance + fallback)
        └── render.py      # FFmpeg cut + center-crop + caption
```

### 5.5 API
| Method | Path | Fungsi |
|--------|------|--------|
| `POST` | `/jobs` | buat job; `{source_url}` atau file; opsi `{max_clips, lang}` |
| `GET` | `/jobs/{id}` | status + daftar klip + metadata |
| `GET` | `/jobs/{id}/clips/{n}` | download klip |
| `GET` | `/healthz` | health check (untuk Coolify) |

Auth MVP: header `Authorization: Bearer {API_TOKEN}` (single token, cukup untuk pemakaian pribadi). `/healthz` bebas.

---

## 6. Skema Output Moment Detection

LLM **wajib** balikan array JSON ini saja (tanpa teks lain):

```json
[
  {
    "start": 412.5,
    "end": 458.2,
    "score": 87,
    "reason": "konflik + punchline, hook kuat di awal",
    "hook": "Ini alasan kenapa 90% orang salah soal...",
    "title": "Kesalahan #1 yang bikin gagal"
  }
]
```
Aturan prompt: pilih 5–15 segmen 15–60 detik, jangan motong di tengah kalimat, `score` 0–100, `hook` 1 baris, **JSON valid saja** tanpa markdown fence/penjelasan.

---

## 7. Step-by-Step Deploy di Coolify

> Asumsi: Coolify sudah jalan di VPS, repo `simbioclip` ada di GitHub, endpoint router LLM sudah aktif.

### Langkah 0 — File deployment di repo

`Dockerfile`:
```dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y ffmpeg git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# torch CPU (lewati jika full Mode A tanpa whisper lokal)
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

`docker-compose.yaml`:
```yaml
services:
  api:
    build: .
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000
    environment:
      - REDIS_URL=redis://redis:6379/0
      - API_TOKEN=${API_TOKEN}
      - LLM_ROUTERS=${LLM_ROUTERS}
      - STT_MODE=${STT_MODE}
      - STT_BASE_URL=${STT_BASE_URL}
      - STT_API_KEY=${STT_API_KEY}
    volumes:
      - clip_data:/app/data
    depends_on: [redis]
    ports:
      - "8000:8000"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')"]
      interval: 30s
      timeout: 5s
      retries: 3

  worker:
    build: .
    command: python -m app.worker
    environment:
      - REDIS_URL=redis://redis:6379/0
      - LLM_ROUTERS=${LLM_ROUTERS}
      - STT_MODE=${STT_MODE}
      - STT_BASE_URL=${STT_BASE_URL}
      - STT_API_KEY=${STT_API_KEY}
    volumes:
      - clip_data:/app/data
    depends_on: [redis]
    deploy:
      resources:
        limits:
          memory: 6G        # jaga agar tidak OOM di VPS 8GB

  redis:
    image: redis:7-alpine
    volumes:
      - redis_data:/data

volumes:
  clip_data:
  redis_data:
```

`.env.example`:
```
API_TOKEN=ganti-token-rahasia
# 9 router sbg JSON: [{"base_url":"...","api_key":"...","model":"..."}, ...]
LLM_ROUTERS=[{"base_url":"https://ai.adzibilal.my.id/v1","api_key":"xxx","model":"..."}]
STT_MODE=local            # 'router' jika offload STT, 'local' untuk faster-whisper
STT_BASE_URL=
STT_API_KEY=
```
Commit & push ke GitHub.

### Langkah 1 — Project & Resource
1. Coolify → **Projects** → **+ New Project** → `simbioclip`.
2. Pilih environment (`production`) → **+ New Resource** → tipe **Docker Compose**.

### Langkah 2 — Hubungkan repo
1. Sumber **GitHub** (authorize bila perlu) → pilih repo `simbioclip`, branch `main`.
2. **Compose file path**: `docker-compose.yaml` → Coolify parse `api`, `worker`, `redis`.

### Langkah 3 — Environment variables
Isi sesuai `.env.example`: `API_TOKEN`, `LLM_ROUTERS` (JSON 9 router), `STT_MODE`, dst.

### Langkah 4 — Persistent storage
`clip_data` & `redis_data` sudah dideklarasi → dipertahankan antar-deploy. Pastikan disk cukup; hapus `source.*` setelah render (lihat §9).

### Langkah 5 — Domain & port
1. Service **api** → set **Domain** (mis. `simbiospace.site`) di tab Domains → SSL otomatis (Let's Encrypt).
2. Jangan ekspos `redis` ke publik.

### Langkah 6 — Resource limit
VPS 8GB: `worker` dibatasi ~6G (sudah di compose). Kalau `STT_MODE=router`, beban turun, boleh longgar.

### Langkah 7 — Deploy & verifikasi
1. Klik **Deploy**, pantau **Logs** tiap service, tunggu healthcheck `api` hijau.
```bash
curl https://simbiospace.site/healthz

curl -X POST https://simbiospace.site/jobs \
  -H "Authorization: Bearer <API_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"source_url":"https://youtu.be/xxxx","max_clips":8}'

curl https://simbiospace.site/jobs/<job_id> \
  -H "Authorization: Bearer <API_TOKEN>"
```
2. (Opsional) Aktifkan **Auto Deploy on push** + webhook GitHub.

---

## 8. Rencana Build MVP (ringkas)

| Tahap | Cakupan | Estimasi |
|-------|---------|----------|
| **B0 — Scaffold** | Repo, Dockerfile, compose, FastAPI `/healthz`, deploy Coolify sukses | Hari 1–2 |
| **B1 — Pipeline inti** | yt-dlp → transkrip → moment LLM → FFmpeg cut center-crop + caption; job async | Hari 3–6 |
| **B2 — Router & dashboard** | Client 9 router + fallback; STT offload opsional; dashboard minimal + download | Hari 7–9 |

---

## 9. Risiko & Mitigasi

| Risiko | Mitigasi |
|--------|----------|
| Free tier router berubah/tutup | Rotasi 9 router + fallback; config via ENV agar gampang ganti |
| VPS OOM (Whisper lokal) | Default Mode A (offload STT); model kecil; memory limit worker |
| Disk penuh oleh video | Hapus `source.*` setelah render; cleanup job lama |
| Rate limit | Antri serial (RQ) + backoff + spread antar router |
| JSON LLM tidak valid | Prompt JSON-only + validasi skema + retry ke router lain |
| Hak cipta konten sumber | Proses hanya konten berizin/lisensi jelas (di luar scope teknis) |

---

*Akhir PRD MVP v1.0 — SimbioClip.*