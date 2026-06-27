Phase 1: S3/SeaweedFS Upload Engine
Goal: Replace public_base_url dependency with direct S3 upload, so Repliz can fetch clips without the app being publicly accessible.
1.1 Add boto3 dependency
- File: requirements.txt
- Tambah boto3>=1.34.0
- Rebuild Docker image
1.2 Create app/integrations/s3_uploader.py
- Class S3Uploader dengan method upload_file(local_path, remote_key) → public_url
- Config dari env: AWS_ENDPOINT, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_BUCKET, AWS_REGION, AWS_USE_PATH_STYLE_ENDPOINT, AWS_FOLDER_NAME
- Support path-style endpoint (SeaweedFS)
- Upload dengan public-read ACL
- Return public URL: {AWS_URL}/{AWS_FOLDER_NAME}/{remote_key}
- Fallback: kalau S3 gagal, fallback ke public_base_url lama
- Timeout 30 menit untuk file besar
1.3 Add S3 env vars to .env.example
# S3/SeaweedFS storage (alternative to PUBLIC_BASE_URL for Repliz)
AWS_ENDPOINT=https://s3.indramusicschool.cloud
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_BUCKET=public
AWS_REGION=us-east-1
AWS_USE_PATH_STYLE_ENDPOINT=true
AWS_FOLDER_NAME=simbioclip
1.4 Add S3 fields to AppSettings (settings_store.py)
- aws_endpoint, aws_access_key_id, aws_secret_key, aws_bucket, aws_region, aws_use_path_style, aws_folder_name
- Load from env, persist in settings.json
- Secret masking untuk aws_secret_key
1.5 Modify build_public_media_urls in repliz.py
- Logic baru: if S3 configured, gunakan s3://... URL
- Jika tidak, fallback ke public_base_url lama
- Tambah function upload_clip_to_s3(clip, job) → (video_url, thumb_url) yang dipanggil di schedule_clip()
1.6 Update repliz_schedule.py
- Di schedule_clip(): sebelum schedule, cek if clip belum di S3, upload dulu
- Update _validate_repliz_config(): allow either public_base_url OR S3 config
- Auto-upload thumbnail juga
1.7 Settings UI untuk S3 (settings.html)
- Tambah section S3 di settings card (expandable/collapsible)
- Fields: Endpoint, Bucket, Region, Folder Name, Access Key, Secret Key, Path Style toggle
- Button "Test S3 Connection"
1.8 API endpoint POST /api/settings/test-s3
- Test koneksi S3 dengan upload file dummy
- Return status
Phase 2: Redesign Repliz Upload Modal
Goal: Unified, rich modal dengan platform icons, AI metadata generation, seperti yt-short-clipper.
2.1 CSS — Repliz design tokens (unify & rebrand)
- File: app/static/css/cards.css (lines 575-679)
- File: app/static/css/tokens.css (tambah token Repliz)
- Ganti semua #3b82f6 (blue) → var(--brand-green) / var(--brand-green-dark)
- Platform color variables untuk setiap platform
- Card style dengan avatar bulat, platform badge, status connected
- Animasi smooth untuk select/deselect
- Schedule section dengan divider visual
- Loading skeleton untuk accounts
2.2 HTML — Unified modal (replaces both base.html & clip_editor.html modals)
- File: app/templates/clip_editor.html (redesign the repliz modal section)
Layout baru (inspired by yt-short-clipper):
┌──────────────────────────────────┐
│  ⬆️ Upload via Repliz       ✕   │
│  Share to multiple platforms     │
├──────────────────────────────────┤
│  📹 Clip preview (title + dur)   │
├──────────────────────────────────┤
│  📝 Post Details                 │
│  ┌─ Title ─────────────────────┐ │
│  │ [Generate with AI 🤖]       │ │
│  └──────────────────────────────┘ │
│  ┌─ Description ───────────────┐ │
│  │ [Generate with AI 🤖]       │ │
│  └──────────────────────────────┘ │
├──────────────────────────────────┤
│  Select Platforms                │
│  [Select All] [Deselect All]     │
│  ┌─────────────────────────────┐ │
│  │ ☐ 🎵 @channel • TIKTOK  ✓ │ │
│  │ ☐ 📺 @channel • YOUTUBE  ✓ │ │
│  │ ☐ 📸 @user • INSTAGRAM  ✗ │ │
│  └─────────────────────────────┘ │
├──────────────────────────────────┤
│  📅 Schedule (max 7 days)        │
│  [DD / MM / YYYY] [HH : MM]     │
│  GMT+7 (WIB)                     │
├──────────────────────────────────┤
│  [Cancel]    [Upload & Schedule] │
└──────────────────────────────────┘
2.3 Remove legacy modal from base.html
- Hapus modal Repliz di base.html lines 414-457
- Hapus JS Repliz di base.html lines 613-687
- Function openReplizModal() tetap di clip_editor.html
2.4 JavaScript — Repliz modal logic (clip_editor.html)
Rewrite JS untuk:
- Platform Icons & Colors: SVG inline per platform (YouTube red, TikTok black, IG pink, FB blue, Threads, LinkedIn)
- Select All / Deselect All buttons
- AI Generate: Tombol yang panggil POST /api/llm/generate-metadata dengan prompt seperti yt-short-clipper
- Schedule picker: datetime-local dengan default +1 jam, validasi max 7 hari
- Timezone info: Tampilkan "(GMT+7 / WIB)"
- Progress tracking: Setelah submit, tampilkan modal progress:
┌───────────────────────────────┐
│  📤 Uploading to Repliz       │
│  ┌─ 🎵 @channel ────────────┐ │
│  │ ✓ Uploaded to storage    │ │
│  │ ✓ Scheduled successfully │ │
│  └──────────────────────────┘ │
│  ┌─ 📺 @channel ────────────┐ │
│  │ ⟳ Uploading to storage   │ │
│  └──────────────────────────┘ │
│  [Close]                      │
└───────────────────────────────┘
- Duplicate check: Grey out + tooltip "Already scheduled" untuk accounts yang sudah ter-schedule
Phase 3: AI-Generated Metadata
Goal: Auto-generate title & description pakai LLM yang sudah terkonfigurasi.
3.1 API endpoint POST /api/llm/generate-repliz-metadata
- File: app/main.py
- Input: clip_id, job_id
- Ambil clip title + hook + transcript snippet
- Panggil LLM router dengan prompt seperti yt-short-clipper
- Return { title, description }
- Timeout 30 detik
3.2 Frontend — "Generate with AI" button
- Di modal Repliz, dua tombol "Generate with AI 🤖" (satu untuk title, satu untuk description)
- Loading state: spinner + "Generating..."
- Auto-fill hasil ke input fields
- Fallback: kalau LLM gagal, pakai title/hook clip
3.3 Prompt design (di frontend JS)
Generate a catchy social media post title and description for this short video clip.

Video Title: {title}
Hook/Content: {hook}
Transcript: {transcript_snippet}

Requirements:
- Title: Max 100 characters, engaging and clickable
- Description: 2-3 sentences, include relevant hashtags

Return JSON: {"title": "...", "description": "..."}
Phase 4: Settings Page — S3 + Repliz Enhancements
4.1 S3 Config Section
- Collapsible card di settings
- Fields: all S3 params
- Button "Test Connection" → POST /api/settings/test-s3
- Mask secret key seperti API keys lainnya
4.2 Repliz Settings Enhancement
- Connected Accounts Preview: Setelah test, tampilkan daftar akun (seperti yt-short-clipper settings)
- Account cards: dengan platform icon, profile picture (kalau ada), status connected
- Default account: Dropdown berisi connected accounts (ganti input manual text)
4.3 API Endpoints
- POST /api/settings/test-s3 — test S3 + return bucket info
- GET /api/repliz/accounts/detailed — sama seperti existing tapi include account count
Phase 5: Backend Pipeline Integration
5.1 Post-render S3 upload
- File: app/pipeline/render.py
- Setelah clip selesai render (line 1416), upload clip + thumbnail ke S3 kalau S3 terkonfigurasi
- Set clip.download_url ke S3 URL
- Auto-schedule Repliz after S3 upload (bukan after render)
5.2 maybe_auto_schedule_clip enhancement
- Upload ke S3 dulu sebelum auto-schedule kalau belum diupload
- Retry logic untuk S3 upload failure
5.3 Cleanup
- Hapus file local setelah S3 upload success? (optional, disk space management)
File Change Summary
File	Action
requirements.txt	Add boto3>=1.34.0
app/integrations/s3_uploader.py	New file
.env.example	Add S3 vars
app/settings_store.py	Add S3 fields
app/integrations/repliz.py	Modify build_public_media_urls, add S3 upload
app/integrations/repliz_schedule.py	Add S3 upload before schedule
app/main.py	Add POST /api/settings/test-s3
app/main.py	Add POST /api/llm/generate-repliz-metadata
app/templates/settings.html	Add S3 section, Repliz accounts preview
app/static/css/cards.css	Redesign Repliz CSS
app/static/css/tokens.css	Add Repliz tokens
app/templates/clip_editor.html	Redesign modal, add AI, progress
app/templates/base.html	Remove legacy Repliz modal
app/templates/partials/job_detail.html	Enhance Repliz badge + button
app/pipeline/render.py	S3 upload after render
app/static/js/app-shell.js	Add Repliz helper functions
Design Assets Needed
Platform icons (inline SVG, brand colors):
- TikTok → #000000
- YouTube → #FF0000
- Instagram → #E1306C
- Facebook → #1877F2
- Threads → #000000
- LinkedIn → #0A66C2
- Twitter/X → #000000
SimbioClip green brand:
- --brand-green: #00b85c
- --brand-green-dark: #00994d
- --brand-green-soft: rgba(0, 184, 92, 0.08)
Execution Order
1. Phase 1 (S3 engine) — backend first, settings UI second
2. Phase 2 (Modal redesign) — CSS → HTML → JS
3. Phase 3 (AI metadata) — API endpoint → frontend button
4. Phase 4 (Settings polish) — S3 UI + Repliz accounts preview
5. Phase 5 (Pipeline) — auto S3 upload + cleanup
