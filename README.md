# SimbioClip

AI video clipper self-hosted — ubah video panjang jadi shorts vertikal dengan caption otomatis. Tanpa biaya langganan.

<img width="1600" height="803" alt="image" src="https://github.com/user-attachments/assets/1fedf842-c73c-4440-8d2d-c5fb5eed3fd6" />

<img width="1901" height="912" alt="image" src="https://github.com/user-attachments/assets/4d92ea0a-711e-4b49-bbd4-72449cf0b11c" />

> **Open Source Project** — This project is open source and still under active development. There may be bugs and issues. Feel free to contribute, report issues, or submit Pull Requests on [GitHub](https://github.com/adzibilal/simbioclip).

## Fitur

- Download video dari URL (YouTube, dll. via yt-dlp) **atau** upload file
- Speech-to-text dengan timestamp per kata (mode API router atau faster-whisper lokal di CPU)
- Deteksi momen terbaik via LLM (skor + hook + judul), **momen lucu/ketawa diprioritaskan lebih dulu**
- Render clip vertikal (9:16 / 1:1 / 4:5 / 16:9) dengan caption ter-burn
- Layout: Auto, Center Crop, Talking Head, Split Cam, Podcast, Inset, Face Track, **BG Blur** (background blur + konten 4:5 tajam + judul menetap)
- Gaya caption: Bold Pop, Karaoke, Neon, Minimal, Podcast
- Edit per-clip: rerender dengan layout/caption berbeda, trim akurat per-frame, **thumbnail cover intro** custom
- Pilihan resolusi download (360p–4K / Best)
- Dashboard web dengan update live (HTMX) + antrean job async (Redis + RQ)

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
   │Download │          │   Router    │     │   Render    │
   └─────────┘          └─────────────┘     └─────────────┘
```

- **API** — FastAPI: menerima job, menyajikan dashboard, streaming status live
- **Worker** — RQ worker: menjalankan pipeline (download → transcribe → deteksi momen → classify → diarize → render)
- **Redis** — antrean job + penyimpanan state
- **LLM Router** — endpoint apa pun yang kompatibel OpenAI; deteksi momen pakai LLM murah/gratis

## Prasyarat

- **Docker** dan **Docker Compose** (satu-satunya syarat wajib — ffmpeg, yt-dlp, font, Chrome sudah ada di dalam image)
- Server/mesin dengan **RAM ≥ 2 GB** (STT mode `router`) atau **RAM ≥ 4 GB** (STT mode `local` menjalankan Whisper di CPU)
- **Endpoint LLM kompatibel OpenAI** untuk deteksi momen (opsi gratis: [Groq](https://console.groq.com), [OpenRouter](https://openrouter.ai), atau LiteLLM sendiri) — lihat **[Cara Dapat API Key LLM Gratis](#cara-dapat-api-key-llm-gratis)**
- *(Opsional tapi disarankan)* **cookies YouTube** format Netscape untuk melewati deteksi bot / pembatasan usia

---

## Instalasi di VPS

Dari VPS Ubuntu/Debian kosong, lengkap dari awal sampai jalan.

### 1. Pasang Docker

```bash
# Pasang Docker Engine + plugin Compose (script resmi)
curl -fsSL https://get.docker.com | sh

# Agar bisa pakai docker tanpa sudo (logout & login lagi setelahnya)
sudo usermod -aG docker "$USER"
newgrp docker

docker --version && docker compose version
```

### 2. Clone proyek

```bash
git clone https://github.com/adzibilal/simbioclip.git
cd simbioclip
```

### 3. Konfigurasi environment

```bash
cp .env.example .env
nano .env        # isi API_TOKEN dan LLM_ROUTERS (lihat bagian "Konfigurasi")
```

Minimal yang wajib diisi:

- `API_TOKEN` — password login dashboard/API (buat yang kuat)
- `LLM_ROUTERS` — minimal satu router kompatibel OpenAI (key Groq/OpenRouter)

### 4. (Opsional) Tambahkan cookies YouTube

Lihat **[Cookies YouTube](#cookies-youtube)**. Di VPS, biasanya cookies di-export dari laptop lalu disalin ke server:

```bash
# dari laptop
scp cookies.txt user@ip-vps-anda:/path/ke/simbioclip/cookies.txt
```

### 5. Build & jalankan

```bash
./run.sh            # ambil GPG key, build image, jalankan stack
# setara dengan: docker compose build && docker compose up -d
```

API berjalan di port **8000**. Cek:

```bash
curl http://localhost:8000/healthz     # -> ok
./run.sh status                          # status & health kontainer
./run.sh logs                            # log live
```

### 6. Buka aksesnya

- **Tes cepat:** buka firewall port 8000 (`ufw allow 8000`) lalu akses `http://IP_VPS_ANDA:8000`.
- **Produksi:** taruh di belakang reverse proxy (Caddy / Nginx / Traefik) dengan HTTPS yang mengarah ke `127.0.0.1:8000`, dan tutup port 8000 dari publik. Atau pakai **[Coolify](#deploy-dengan-coolify)**.

> Data (hasil download, clip, thumbnail) tersimpan di volume Docker `clip_data` dan tetap ada setelah restart. State Redis ada di `redis_data`.

---

## Instalasi di Local

Untuk development atau jalan di mesin sendiri.

1. **Pasang Docker Desktop** (macOS/Windows) atau Docker Engine (Linux).
2. Clone dan konfigurasi:

   ```bash
   git clone https://github.com/adzibilal/simbioclip.git
   cd simbioclip
   cp .env.example .env
   # edit .env: API_TOKEN + LLM_ROUTERS
   ```

3. *(Opsional)* taruh `cookies.txt` di root proyek — lihat **[Cookies YouTube](#cookies-youtube)**.
4. Jalankan:

   ```bash
   ./run.sh
   ```

5. Buka **http://localhost:8000** dan login dengan `API_TOKEN` Anda.

Perintah dev yang berguna (lihat `./run.sh help`):

```bash
./run.sh logs worker      # pantau worker
./run.sh restart          # restart api + worker (tanpa rebuild)
./run.sh rebuild          # rebuild penuh --no-cache (setelah ubah kode)
./run.sh shell worker     # masuk shell kontainer worker
```

> **Perubahan kode** (Python/template) di-bake ke dalam image, jadi rebuild setelah mengedit:
> `docker compose up -d --build`. File `.env` dan `cookies.txt` dibaca live (tidak perlu rebuild).

---

## Konfigurasi

Edit `.env`. Yang paling penting:

### `LLM_ROUTERS` (wajib)

Array JSON berisi endpoint-endpoint kompatibel OpenAI. Dicoba **berurutan** sebagai fallback (jika satu gagal/kena limit, lanjut ke berikutnya).

```json
[
  {"base_url": "https://api.groq.com/openai/v1", "api_key": "gsk_xxx", "model": "llama-3.3-70b-versatile"},
  {"base_url": "https://openrouter.ai/api/v1", "api_key": "sk-or-xxx", "model": "google/gemini-2.0-flash-exp:free"}
]
```

Tulis dalam satu baris di `.env`:

```
LLM_ROUTERS=[{"base_url":"https://api.groq.com/openai/v1","api_key":"gsk_xxx","model":"llama-3.3-70b-versatile"}]
```

### Speech-to-text (`STT_MODE`)

- `STT_MODE=local` — menjalankan faster-whisper di CPU dalam kontainer. Tanpa API key, tapi butuh **RAM ≥ 4 GB** dan lebih lambat.
- `STT_MODE=router` — mengalihkan transkripsi ke API kompatibel-Whisper. Isi `STT_BASE_URL`, `STT_API_KEY`, `STT_MODEL` (Whisper gratis dari Groq `whisper-large-v3-turbo` sangat bagus; untuk OpenAI pakai `STT_MODEL=whisper-1`).

---

## Cara Dapat API Key LLM Gratis

SimbioClip butuh setidaknya satu endpoint LLM kompatibel-OpenAI untuk deteksi momen. Berikut beberapa penyedia gratis dan tipsnya. **Catatan:** nama model sering berubah — selalu cek daftar model terbaru di dashboard masing-masing penyedia.

### Groq — rekomendasi (cepat & gratis)

1. Buka **https://console.groq.com** lalu daftar (bisa pakai Google/GitHub).
2. Menu **API Keys** → **Create API Key** → salin (formatnya `gsk_...`).
3. Pakai di `.env`:
   - `base_url`: `https://api.groq.com/openai/v1`
   - `model` (LLM): `llama-3.3-70b-versatile` (kualitas bagus) atau `llama-3.1-8b-instant` (cepat & hemat)
4. **Bonus:** Groq juga menyediakan **Whisper gratis** untuk STT. Set:
   ```
   STT_MODE=router
   STT_BASE_URL=https://api.groq.com/openai/v1
   STT_API_KEY=gsk_xxx
   STT_MODEL=whisper-large-v3-turbo
   STT_MODEL_FALLBACK=whisper-large-v3
   ```
5. Groq punya **rate limit harian** — kalau sering kena limit, tambahkan router lain sebagai cadangan (lihat di bawah).

### OpenRouter — banyak model gratis

1. Buka **https://openrouter.ai** → daftar.
2. Menu **Keys** → **Create Key** → salin (formatnya `sk-or-...`).
3. Pakai di `.env`:
   - `base_url`: `https://openrouter.ai/api/v1`
   - `model`: pilih yang berakhiran **`:free`**, misalnya `google/gemini-2.0-flash-exp:free` atau `meta-llama/llama-3.3-70b-instruct:free` (cek halaman **Models** → filter "Free").

### Google AI Studio (Gemini)

1. Buka **https://aistudio.google.com/app/apikey** → **Create API key** (gratis).
2. Pakai endpoint kompatibel-OpenAI dari Google:
   - `base_url`: `https://generativelanguage.googleapis.com/v1beta/openai/`
   - `model`: `gemini-2.0-flash` atau `gemini-1.5-flash`

### Penyedia gratis lain

Cerebras, Mistral (free tier), Together (kredit trial), GitHub Models — semuanya kompatibel-OpenAI, tinggal masukkan `base_url`, `api_key`, `model` masing-masing ke dalam array `LLM_ROUTERS`.

### Menyusun banyak router (fallback otomatis)

`LLM_ROUTERS` adalah **array** — sistem mencoba router pertama, dan kalau gagal atau kena rate-limit ia otomatis lanjut ke router berikutnya. Susun beberapa penyedia gratis sekaligus supaya tahan terhadap limit harian:

```
LLM_ROUTERS=[{"base_url":"https://api.groq.com/openai/v1","api_key":"gsk_xxx","model":"llama-3.3-70b-versatile"},{"base_url":"https://openrouter.ai/api/v1","api_key":"sk-or-xxx","model":"meta-llama/llama-3.3-70b-instruct:free"},{"base_url":"https://generativelanguage.googleapis.com/v1beta/openai/","api_key":"AIza_xxx","model":"gemini-2.0-flash"}]
```

> Urutkan dari yang paling cepat/berkualitas di depan. Anda boleh menumpuk hingga banyak router (mis. 9) sebagai rantai cadangan.

---

## Cookies YouTube

YouTube makin sering memblokir IP server ("Sign in to confirm you're not a bot") dan mengunci sebagian video. Memberi cookies akun yang sudah login akan mengatasinya. Metode yang andal dan didukung adalah **`cookies.txt` format Netscape** yang di-mount ke kontainer (sudah otomatis tersambung di `docker-compose.yaml` → `COOKIES_FILE=/app/cookies.txt`).

### Langkah 1 — Export cookies dari browser

1. Login ke **youtube.com** di Chrome atau Firefox.
2. Pasang ekstensi **“Get cookies.txt LOCALLY”** ([Chrome Web Store](https://chromewebstore.google.com/) — cari nama persisnya).
3. Di tab YouTube, klik ekstensi → **Export** → terunduh file `cookies.txt` (format Netscape).

### Langkah 2 — Salin ke proyek

File **harus** bernama `cookies.txt` dan berada di **root proyek** (di-mount ke `/app/cookies.txt`):

```bash
# local
cp ~/Downloads/cookies.txt ./cookies.txt

# VPS (dari laptop)
scp ~/Downloads/cookies.txt user@ip-vps-anda:/path/ke/simbioclip/cookies.txt
```

### Langkah 3 — Terapkan

File cookies dibaca **live** saat download — tidak perlu rebuild. Kalau kontainer sudah jalan sebelum file ada, cukup restart agar mount-nya segar:

```bash
./run.sh restart
```

> **Catatan**
> - Cookies YouTube punya masa berlaku — kalau download mulai gagal dengan error autentikasi, export ulang dan ganti `cookies.txt`.
> - `cookies.txt` di-git-ignore; jangan pernah di-commit. Perlakukan seperti password.
> - Cookies opsional untuk banyak video publik, tapi wajib untuk video dengan batasan usia dan untuk menghindari deteksi bot pada IP server yang sibuk.

---

## Langkah demi Langkah: Membuat Clip Pertama

> Lakukan **[Cookies YouTube](#cookies-youtube)** dulu kalau clip dari YouTube.

1. **Buka dashboard** → `http://localhost:8000` (local) atau URL VPS Anda.
2. **Login** dengan `API_TOKEN` Anda.
3. **Tambahkan sumber** — tempel **Video URL** (mis. link YouTube) atau **upload** file video mentah.
   - *(Opsional)* klik **Preview** untuk mengambil judul/durasi sebelum diproses.
4. **Pilih opsi:**

   | Opsi | Fungsinya |
   |------|-----------|
   | **Max clips** | Berapa banyak clip yang dihasilkan (1–15). |
   | **Layout** | Gaya framing: `Auto`, `Podcast`, `Talking Head`, `Center Crop`, `Game Stream`, **`BG Blur`**. |
   | **Ratio** | Rasio aspek output: `9:16`, `1:1`, `4:5`, `16:9`. |
   | **Lang** | Bahasa caption/transkrip (`ID`, `EN`, `ES`, atau `Auto`). |
   | **Caption** | Gaya subtitle: `Bold Pop`, `Karaoke`, `Neon`, `Minimal`, `Podcast`. |
   | **Duration** | Rentang target durasi clip (mis. `45–60s`). |
   | **Res** | Resolusi download sumber (`Best`, `4K`…`360p`). |

5. **Submit.** Job berjalan melalui pipeline secara live: **download → transcribe → moments → classify → diarize → render**. Pantau progres di kartu job.
6. **Tinjau clip.** Tiap clip jadi menampilkan preview, skor, hook, dan layout yang terdeteksi.
7. **Sempurnakan (opsional):**
   - **Rerender** satu clip dengan **layout** atau **gaya caption** berbeda (ini cara termudah menerapkan **BG Blur** ke satu clip).
   - **Trim** — penyesuaian awal/akhir akurat per-frame, non-destruktif.
   - **Thumbnail** — upload gambar custom; ia ditambahkan sebagai **cover intro** singkat (~0,2 dtk) di depan clip. Caption tetap aman.
   - **Edit** teks judul/hook.
8. **Download** clip-nya, atau ambil thumbnail JPG yang dibuat otomatis.

### Tentang layout **BG Blur**

Background blur memenuhi frame, dengan crop **4:5** konten yang tajam di tengah, **judul clip menetap di atas**, dan **subtitle di bawah**. Pilih sebagai Layout saat membuat job, atau **Rerender** clip yang sudah ada lalu pilih **BG Blur**.

### Deteksi momen "funny-first"

Detektor momen memburu momen **lucu / ketawa** lebih dulu (lelucon, punchline, orang ngakak), dan hanya mengisi sisa slot dengan momen kuat lain (insight, cerita, hot take) ketika momen lucu tidak cukup.

---

## Deploy dengan Coolify

Pakai ini kalau ingin HTTPS + domain terkelola di VPS.

1. Coolify → **Projects** → **+ New Project** → `simbioclip`
2. **+ New Resource** → **Docker Compose**
3. Source **GitHub** → pilih `adzibilal/simbioclip`, branch `main`
4. **Compose file path**: `docker-compose.coolify.yaml`
5. Isi environment variables (`API_TOKEN`, `LLM_ROUTERS`, `STT_*`) di tab **Environment**
6. Set domain untuk service **api** di tab **Domains** (compose sudah mendeklarasikan `SERVICE_FQDN_API_8000`)
7. **Deploy**

> Compose Coolify **tidak** mem-bind-mount `cookies.txt`. Untuk memakai cookies di sana, tambahkan entri persistent storage yang memetakan sebuah `cookies.txt` ke `/app/cookies.txt`, lalu set `COOKIES_FILE=/app/cookies.txt` di tab Environment.

---

## Variabel Environment

| Variabel | Default | Deskripsi |
|----------|---------|-----------|
| `API_TOKEN` | `super-secret-admin-token` | Token auth dashboard & API. **Wajib diganti.** |
| `REDIS_URL` | `redis://redis:6379/0` | String koneksi Redis |
| `LLM_ROUTERS` | array JSON | Daftar router kompatibel-OpenAI untuk deteksi momen (dicoba berurutan) |
| `STT_MODE` | `local` | `router` (API) atau `local` (faster-whisper di CPU) |
| `STT_BASE_URL` | `""` | URL dasar API STT (mode router) |
| `STT_API_KEY` | `""` | API key STT (mode router) |
| `STT_MODEL` | `whisper-large-v3-turbo` | Model STT utama |
| `STT_MODEL_FALLBACK` | `whisper-large-v3` | Model STT cadangan |
| `LLM_TIMEOUT` | `240` | Timeout per request LLM (detik) |
| `LLM_MAX_RETRIES` | `1` | Maksimum retry LLM |
| `COOKIES_FILE` | `/app/cookies.txt` (compose) | Path ke cookies.txt format Netscape |
| `CONCURRENT_FRAGMENTS` | `5` | Jumlah fragment download paralel (yt-dlp) |
| `ARIA2C_ENABLED` | `true` | Pakai downloader eksternal aria2c (16 koneksi paralel) |
| `ARIA2C_CONNECTIONS` | `16` | Koneksi aria2c per file |
| `THROTTLED_RATE` | `200M` | Ambang bypass throttle yt-dlp |

## Endpoint API

| Method | Path | Auth | Deskripsi |
|--------|------|------|-----------|
| `POST` | `/login` | - | Login (form `token`) |
| `GET` | `/` | Cookie | Dashboard |
| `GET` | `/healthz` | - | Health check |
| `POST` | `/preview` | Bearer | Preview metadata video |
| `POST` | `/jobs` | Bearer | Buat job baru |
| `GET` | `/jobs/{id}` | Bearer | Status job + clip |
| `DELETE` | `/jobs/{id}` | Bearer | Hapus job |
| `POST` | `/jobs/{id}/retry` | Bearer | Ulangi job |
| `POST` | `/jobs/{id}/retry/{step}` | Bearer | Ulangi dari step tertentu |
| `POST` | `/jobs/{id}/rerender` | Bearer | Render ulang semua clip |
| `POST` | `/jobs/{id}/clips/{cid}/rerender` | Bearer | Render ulang satu clip |
| `POST` | `/jobs/{id}/clips/{cid}/trim` | Bearer | Trim sebuah clip |
| `POST` | `/api/jobs/{id}/clips/{cid}/custom-thumbnail` | Bearer | Upload thumbnail cover custom |
| `GET` | `/jobs/{id}/clips/{cid}` | Bearer | Download sebuah clip |
| `POST` | `/cleanup` | Bearer | Hapus job lama |

### Contoh: buat job via API

```bash
curl -X POST https://clip.example.com/jobs \
  -H "Authorization: Bearer $API_TOKEN" \
  -F "source_url=https://youtube.com/watch?v=xxxx" \
  -F "max_clips=8" \
  -F "layout_mode=bg_blur" \
  -F "aspect_ratio=9:16" \
  -F "caption_style=karaoke_highlight" \
  -F "clip_duration=45-60" \
  -F "download_resolution=1080p"
```

## Tahapan Pipeline

| Step | Deskripsi | Bisa diulang |
|------|-----------|--------------|
| `download` | Download video via yt-dlp (pakai cookies bila ada) | Ya |
| `transcribe` | Speech-to-text + pembersihan | Ya |
| `moments` | LLM menilai momen layak-clip (funny-first) | Ya |
| `classify` | Deteksi tipe konten / layout | Ya |
| `diarize` | Identifikasi giliran bicara | Ya |
| `render` | Reframe + burn caption + thumbnail cover opsional | Ya |

## Pemecahan Masalah (Troubleshooting)

- **`Sign in to confirm you're not a bot` / download gagal** → tambah atau perbarui **[cookies.txt](#cookies-youtube)**.
- **Tidak ada clip / hasil kosong** → pastikan `LLM_ROUTERS` JSON-nya valid dan key-nya berfungsi; pantau `./run.sh logs worker`.
- **Worker kena OOM di STT mode `local`** → ganti ke `STT_MODE=router`, atau beri RAM lebih ke worker (compose membatasi di 6 GB).
- **Peringatan `cookies.txt is a directory`** → `run.sh` memperbaikinya otomatis, tapi pastikan Anda menyalin *file*, bukan membuat folder.
- **Sudah ubah kode tapi tidak berubah** → rebuild: `docker compose up -d --build` (kode di-bake ke image).

## Pengembangan (Development)

```bash
./run.sh rebuild          # rebuild penuh tanpa cache
./run.sh logs             # pantau semua log
./run.sh shell worker     # shell di dalam worker
./run.sh restart          # restart service tanpa rebuild
./run.sh clean            # hapus kontainer, image, dan volume clip_data
```
