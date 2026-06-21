import os
import json
import uuid
import glob
import shutil
import asyncio
import subprocess
import logging
from datetime import datetime
from typing import Optional, List
from fastapi import FastAPI, Depends, HTTPException, Header, Query, Cookie, Form, File, UploadFile, Request, status, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from redis import Redis
from rq import Queue

from app.config import API_TOKEN, REDIS_URL, DATA_DIR, COOKIES_FILE
from app.models import Job, Clip, ClipCropOverrides, ClipSubtitleEdit, SubtitleStyleOverrides, Composition, CompositionClip, PIPELINE_STEPS, CLIP_DURATION_PRESETS
from app.pipeline.orchestrator import process_video_job, reset_job_step
from app.pipeline.render import render_job_clips, render_one_clip, CAPTION_STYLES
from app.pipeline.download import download_job_video

# Configure logger
logger = logging.getLogger("simbioclip.api")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="SimbioClip API", version="1.0.0")

# Setup template renderer
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

def render_template_str(template_name: str, **kwargs) -> str:
    """Renders a Jinja2 template to string (no Request object needed)."""
    template = templates.env.get_template(template_name)
    return template.render(**kwargs)

# Initialize Redis & RQ
redis_conn = Redis.from_url(REDIS_URL)
job_queue = Queue("default", connection=redis_conn)

# Auth Helper
def verify_token(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
    api_token: Optional[str] = Cookie(None)
) -> str:
    """
    Verifies that the request has the correct API token.
    Checks Bearer header, URL query param, and Cookies.
    """
    # 1. Check Bearer Authorization Header
    if authorization and authorization.startswith("Bearer "):
        provided_token = authorization.split(" ")[1]
        if provided_token == API_TOKEN:
            return API_TOKEN

    # 2. Check Query parameter
    if token == API_TOKEN:
        return API_TOKEN

    # 3. Check Cookie (useful for browser dashboard)
    if api_token == API_TOKEN:
        return API_TOKEN

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized: Invalid API Token"
    )

# HTML routes
@app.get("/", response_class=HTMLResponse)
async def get_landing_page(request: Request):
    """Renders the public landing page."""
    return templates.TemplateResponse(request, "landing.html", {})

@app.get("/app", response_class=HTMLResponse)
async def get_dashboard(request: Request, api_token: Optional[str] = Cookie(None)):
    """Renders the dashboard index page if logged in, else renders the login page."""
    if api_token != API_TOKEN:
        return templates.TemplateResponse(request, "login.html", {"error": None})
    
    # Load all jobs to display in dashboard
    jobs = Job.get_all()
    return templates.TemplateResponse(request, "index.html", {"jobs": jobs, "api_token": API_TOKEN})

@app.post("/login")
async def do_login(request: Request, token: str = Form(...)):
    """Handles authentication and sets a cookie on success."""
    if token == API_TOKEN:
        response = RedirectResponse(url="/app", status_code=status.HTTP_303_SEE_OTHER)
        # Set persistent token cookie (lasts for 30 days)
        response.set_cookie(key="api_token", value=API_TOKEN, max_age=30*24*60*60, httponly=True)
        return response
    
    return templates.TemplateResponse(request, "login.html", {"error": "Invalid Token"})

@app.get("/logout")
async def do_logout():
    """Logs out user by clearing the authentication cookie."""
    response = RedirectResponse(url="/app", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("api_token")
    return response

# WebSocket — real-time job updates
@app.websocket("/ws/job/{job_id}")
async def websocket_job_updates(websocket: WebSocket, job_id: str):
    await websocket.accept()
    last_status = None
    last_clips_len = 0
    last_updated_at = None
    try:
        while True:
            job = Job.load(job_id)
            if job:
                changed = job.status != last_status or len(job.clips) != last_clips_len or job.updated_at != last_updated_at
                if changed:
                    last_status = job.status
                    last_clips_len = len(job.clips)
                    last_updated_at = job.updated_at
                    html = render_template_str("partials/job_detail.html", job=job, api_token=API_TOKEN)
                    await websocket.send_text(html)
                if job.status in ("done", "failed"):
                    await asyncio.sleep(1)
                    # Send final update, then close
                    html = render_template_str("partials/job_detail.html", job=job, api_token=API_TOKEN)
                    try:
                        await websocket.send_text(html)
                    except Exception:
                        pass
                    break
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket error for job {job_id}: {e}")
    finally:
        try:
            await websocket.close()
        except Exception:
            pass

# SSE — real-time pipeline log streaming
@app.get("/api/jobs/{job_id}/logs/stream")
async def stream_job_logs(job_id: str, api_token: Optional[str] = Cookie(None)):
    if api_token != API_TOKEN:
        return HTMLResponse("Unauthorized", status_code=401)
    job = Job.load(job_id)
    if not job:
        return HTMLResponse("Not found", status_code=404)

    log_path = os.path.join(DATA_DIR, "jobs", job_id, "pipeline.log")

    async def generate():
        pos = 0
        # Flush all existing lines first
        if os.path.exists(log_path):
            with open(log_path, "rb") as f:
                data = f.read()
                pos = len(data)
            for line in data.decode("utf-8", errors="replace").splitlines():
                if line.strip():
                    yield f"data: {line}\n\n"

        # Then tail for new lines until job completes
        while True:
            j = Job.load(job_id)
            if os.path.exists(log_path):
                try:
                    with open(log_path, "rb") as f:
                        f.seek(pos)
                        chunk = f.read()
                    if chunk:
                        pos += len(chunk)
                        for line in chunk.decode("utf-8", errors="replace").splitlines():
                            if line.strip():
                                yield f"data: {line}\n\n"
                except Exception:
                    pass
            if j and j.status in ("done", "failed", "cancelled"):
                yield 'data: {"done":true}\n\n'
                break
            await asyncio.sleep(1)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# Health Check API
@app.get("/healthz")
async def health_check():
    """Simple health check endpoint for monitoring."""
    try:
        redis_conn.ping()
        redis_status = "connected"
    except Exception as e:
        redis_status = f"error: {e}"
    
    return {
        "status": "ok",
        "redis": redis_status
    }

# Jobs API
def _extract_youtube_id(url: str) -> Optional[str]:
    import re
    for p in [
        r'(?:youtube\.com/watch\?.*v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})',
    ]:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None

@app.post("/preview")
async def preview_video(source_url: str = Form(...), _: str = Depends(verify_token)):
    """Fetch video metadata for preview before enqueuing."""
    import re
    match = re.search(r'(https?://[^\s]+)', source_url.strip())
    if match:
        source_url = match.group(1)
    try:
        cmd = [
            "yt-dlp", "--dump-json", "--no-download",
            "--no-playlist", "--quiet", "--no-warnings",
            "--js-runtimes", "node",
        ]
        if COOKIES_FILE and os.path.exists(COOKIES_FILE):
            with open(COOKIES_FILE) as _f:
                _content = _f.read()
            if any(_line.strip() and '\t' in _line for _line in _content.splitlines()):
                cmd += ["--cookies", COOKIES_FILE]
        cmd.append(source_url)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if result.returncode != 0:
            raise RuntimeError(result.stderr[:200])
        data = json.loads(result.stdout)
        webpage_url = data.get("webpage_url", source_url)
        formats = data.get("formats", [])
        seen = set()
        available_res = []
        for f in formats:
            h = f.get("height")
            if h and h not in seen and h <= 2160:
                seen.add(h)
                label = f"{h}p"
                available_res.append({"value": label, "label": label})
        available_res.sort(key=lambda x: int(x["value"].replace("p", "")), reverse=True)
        return {
            "title": data.get("title", "Unknown"),
            "duration": data.get("duration", 0),
            "thumbnail": data.get("thumbnail", ""),
            "webpage_url": webpage_url,
            "video_id": _extract_youtube_id(webpage_url),
            "available_resolutions": available_res,
        }
    except Exception as e:
        logger.error(f"Preview failed for {source_url}: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to fetch video info: {str(e)}")

@app.get("/api/cookies")
async def get_cookies(_: str = Depends(verify_token)):
    """Return current cookies file content."""
    if not COOKIES_FILE or not os.path.exists(COOKIES_FILE):
        return {"cookies": ""}
    try:
        with open(COOKIES_FILE) as f:
            content = f.read()
        return {"cookies": content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read cookies: {str(e)}")

@app.post("/api/update-cookies")
async def update_cookies(cookies: str = Form(...), _: str = Depends(verify_token)):
    """Save new cookies content to the cookies file."""
    if not COOKIES_FILE:
        raise HTTPException(status_code=500, detail="COOKIES_FILE path not configured")
    lines = cookies.strip().splitlines()
    has_netscape = any("# Netscape" in line for line in lines)
    has_youtube = any(".youtube.com" in line for line in lines)
    if not has_netscape or not has_youtube:
        raise HTTPException(status_code=400, detail="Invalid cookies format. Expected Netscape format with .youtube.com entries.")
    try:
        with open(COOKIES_FILE, "w") as f:
            f.write(cookies)
        logger.info(f"Cookies updated ({len(lines)} lines)")
        return {"ok": True, "lines": len(lines)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write cookies: {str(e)}")

# ── YouTube Auth ────────────────────────────────────────────────────────────

CHROME_PROFILE = "/app/data/chrome-profile"


def _test_cookies(filepath: str) -> bool:
    """Return True if cookies file exists and works against a YouTube video."""
    if not filepath or not os.path.exists(filepath):
        return False
    with open(filepath) as f:
        content = f.read()
    if "__Secure-3PSID" not in content:
        return False
    result = subprocess.run(
        [
            "yt-dlp",
            "--cookies", filepath,
            "--extractor-args", "youtube:player_client=android,web",
            "--skip-download", "--quiet",
            "--print", "title",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ],
        capture_output=True, text=True, timeout=30,
    )
    return result.returncode == 0


@app.get("/api/auth/youtube")
async def auth_youtube_check(_: str = Depends(verify_token)):
    """Check YouTube connection status."""
    return {"connected": _test_cookies(COOKIES_FILE)}


@app.post("/api/auth/youtube/refresh")
async def auth_youtube_refresh(_: str = Depends(verify_token)):
    """Re-extract cookies from Chrome profile (if user logged in there)."""
    if not COOKIES_FILE:
        raise HTTPException(status_code=500, detail="COOKIES_FILE not configured")
    result = subprocess.run(
        [
            "yt-dlp",
            "--cookies-from-browser", f"chrome:{CHROME_PROFILE}",
            "--cookies", COOKIES_FILE,
            "--skip-download", "--quiet",
            "--print", "title",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0 and _test_cookies(COOKIES_FILE):
        with open(COOKIES_FILE) as f:
            lines = len(f.read().strip().splitlines())
        logger.info(f"Cookies refreshed from Chrome ({lines} lines)")
        return {"ok": True, "lines": lines}
    detail = result.stderr[:500] if result.stderr else "No cookies in Chrome profile"
    raise HTTPException(status_code=400, detail=detail)


@app.post("/jobs")
async def create_job(
    source_url: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    max_clips: int = Form(5),
    lang: Optional[str] = Form(None),
    layout_mode: str = Form("auto"),
    aspect_ratio: str = Form("9:16"),
    audio_ducking: bool = Form(False),
    clip_start: Optional[float] = Form(None),
    clip_end: Optional[float] = Form(None),
    caption_style: str = Form("bold_pop"),
    clip_duration: str = Form("auto"),
    dense_cut: bool = Form(False),
    download_resolution: str = Form("1080p"),
    _: str = Depends(verify_token)
):
    """
    Creates and enqueues a new video clipping job.
    Accepts source_url OR file upload.
    """
    if not source_url and not file:
        raise HTTPException(status_code=400, detail="Either source_url or file upload is required")

    if aspect_ratio not in ("9:16", "1:1", "4:5", "16:9"):
        raise HTTPException(status_code=400, detail=f"Invalid aspect_ratio: {aspect_ratio}")

    valid_styles = ("bold_pop", "neon", "minimal", "karaoke_highlight", "podcast")
    if caption_style not in valid_styles:
        raise HTTPException(status_code=400, detail=f"Invalid caption_style: {caption_style}")

    if clip_duration not in CLIP_DURATION_PRESETS:
        raise HTTPException(status_code=400, detail=f"Invalid clip_duration: {clip_duration}")

    valid_resolutions = ("best", "2160p", "1440p", "1080p", "720p", "480p", "360p")
    if download_resolution not in valid_resolutions:
        raise HTTPException(status_code=400, detail=f"Invalid download_resolution: {download_resolution}")

    if source_url:
        import re
        match = re.search(r'(https?://[^\s]+)', source_url.strip())
        if match:
            source_url = match.group(1)
            
    if clip_start is not None and clip_end is not None:
        if clip_start == 0 and clip_end == 0:
            clip_start = None
            clip_end = None
        elif clip_end <= clip_start:
            raise HTTPException(status_code=400, detail="clip_end must be > clip_start")

    job_id = str(uuid.uuid4())
    job = Job(id=job_id, max_clips=max_clips, lang=lang, layout_mode=layout_mode,
              aspect_ratio=aspect_ratio, audio_ducking=audio_ducking,
              clip_start=clip_start, clip_end=clip_end,
              caption_style=caption_style, clip_duration=clip_duration, dense_cut=dense_cut,
              download_resolution=download_resolution)
    
    # Setup job directory
    job_dir = job.get_dir()
    
    if file:
        # Save uploaded file
        # Retain extension, default to .mp4 if not detected
        original_ext = os.path.splitext(file.filename)[1] if file.filename else ".mp4"
        if not original_ext:
            original_ext = ".mp4"
            
        file_path = os.path.join(job_dir, f"source{original_ext}")
        logger.info(f"Saving uploaded file for job {job_id} to {file_path}")
        
        try:
            with open(file_path, "wb") as buffer:
                # Read chunks to handle large files efficiently
                while chunk := await file.read(1024 * 1024):
                    buffer.write(chunk)
            job.file_name = file.filename
        except Exception as e:
            logger.error(f"Failed to save uploaded file: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to upload file: {e}")
    else:
        # Save URL
        job.source_url = source_url
        # Set thumbnail URL from YouTube if possible
        video_id = _extract_youtube_id(source_url)
        if video_id:
            job.thumbnail_url = f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg"
        
    # Persist the job details
    job.save()
    
    # Enqueue background task
    # timeout: 2 hours (handles large videos)
    job_queue.enqueue(process_video_job, job_id, job_timeout="2h")
    
    logger.info(f"Enqueued clipping job {job_id}")
    
    # Return JSON response
    return {"message": "Job enqueued successfully", "job_id": job_id}

@app.get("/jobs/{job_id}")
async def get_job_status(job_id: str, _: str = Depends(verify_token)):
    """Retrieves status and clips of a specific job."""
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job

@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str, _: str = Depends(verify_token)):
    """Deletes a job metadata and all associated local files (clips/source).
    Cancels the RQ job if still running or queued."""
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Mark as cancelled so running workers abort early
    job_dir = job.get_dir()
    cancel_marker = os.path.join(job_dir, ".cancelled")
    try:
        with open(cancel_marker, "w") as f:
            f.write("cancelled")
    except Exception:
        pass

    job.status = "cancelled"
    job.error = "Cancelled by user"
    try:
        job.save()
    except Exception:
        pass

    # Cancel any pending or running RQ job
    try:
        from redis import Redis
        from rq import Queue
        r = Redis.from_url(REDIS_URL)
        q = Queue("default", connection=r)
        rq_jobs = q.get_jobs()
        for rq_job in rq_jobs:
            if rq_job.args and len(rq_job.args) > 0 and str(rq_job.args[0]) == job_id:
                rq_job.cancel()
                logger.info(f"Cancelled RQ job for {job_id}")
                break
    except Exception as e:
        logger.warning(f"Failed to cancel RQ job for {job_id}: {e}")

    try:
        if os.path.exists(job_dir):
            shutil.rmtree(job_dir)
        logger.info(f"Successfully deleted files for job {job_id}")
        return {"message": f"Job {job_id} deleted successfully"}
    except Exception as e:
        logger.error(f"Failed to delete job files: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete job files: {e}")


@app.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, _: str = Depends(verify_token)):
    """Cancels a running or queued job without deleting its files
    so it can be retried later."""
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    clean_status = job.status.split(" ")[0]
    if clean_status not in ("queued", "downloading", "transcribing", "finding_moments",
                             "classifying", "diarizing", "rendering"):
        raise HTTPException(status_code=400, detail="Job is not running")

    job_dir = job.get_dir()
    cancel_marker = os.path.join(job_dir, ".cancelled")
    try:
        with open(cancel_marker, "w") as f:
            f.write("cancelled")
    except Exception:
        pass

    job.status = "cancelled"
    job.error = "Cancelled by user"
    try:
        job.save()
    except Exception:
        pass

    # Cancel any pending or running RQ job
    try:
        from redis import Redis
        from rq import Queue
        r = Redis.from_url(REDIS_URL)
        q = Queue("default", connection=r)
        rq_jobs = q.get_jobs()
        for rq_job in rq_jobs:
            if rq_job.args and len(rq_job.args) > 0 and str(rq_job.args[0]) == job_id:
                rq_job.cancel()
                logger.info(f"Cancelled RQ job for {job_id}")
                break
    except Exception as e:
        logger.warning(f"Failed to cancel RQ job for {job_id}: {e}")

    html = render_template_str("partials/job_detail.html", job=job, api_token=API_TOKEN)
    return HTMLResponse(html)

@app.post("/jobs/{job_id}/retry")
async def retry_job(job_id: str, _: str = Depends(verify_token)):
    """Resets a failed job and re-enqueues it for processing."""
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    all_clips_empty = all(c.file_path is None for c in job.clips)
    if job.status not in ("failed", "done", "cancelled") or (job.status == "done" and not all_clips_empty):
        raise HTTPException(status_code=400, detail="Only failed or empty done jobs can be retried")

    # Clean up old rendered clips; keep source.mp4, segments*.json, diarization.json
    # so retry can resume from the failed step instead of re-downloading / re-transcribing.
    clips_dir = os.path.join(job.get_dir(), "clips")
    if os.path.exists(clips_dir):
        shutil.rmtree(clips_dir)

    job.status = "queued"
    job.error = None
    job.failed_step = None
    job.clips = []
    job.content_type = None
    job.download_pct = None
    job.download_downloaded_mb = None
    job.download_total_mb = None
    job.save()

    job_queue.enqueue(process_video_job, job_id, job_timeout="2h")
    logger.info(f"Re-enqueued job {job_id} for retry")
    return {"message": "Job retry enqueued successfully", "job_id": job_id}


@app.post("/jobs/{job_id}/retry/{step}")
async def retry_job_step(job_id: str, step: str, _: str = Depends(verify_token)):
    """Re-runs the pipeline from a single step (and everything that depends on it),
    reusing the cached outputs of earlier steps. Returns the refreshed detail partial."""
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    valid_steps = {s["id"] for s in PIPELINE_STEPS}
    if step not in valid_steps:
        raise HTTPException(status_code=400, detail=f"Unknown step '{step}'")

    clean_status = job.status.split(" ")[0]
    if clean_status not in ("done", "failed", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail="Job is still running. Wait for it to finish before retrying a step.",
        )

    reset_job_step(job, step)
    job_queue.enqueue(process_video_job, job_id, job_timeout="2h")
    logger.info(f"Re-enqueued job {job_id} from step '{step}'")

    html = render_template_str("partials/job_detail.html", job=job, api_token=API_TOKEN)
    return HTMLResponse(html)


@app.post("/jobs/{job_id}/rerender")
async def rerender_job(job_id: str, _: str = Depends(verify_token)):
    """Re-renders all clips of a done job without re-downloading or re-transcribing."""
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in ("done", "failed"):
        raise HTTPException(status_code=400, detail="Only done or failed jobs can be re-rendered")

    job_dir = job.get_dir()

    # Check for saved segments
    seg_path = os.path.join(job_dir, "segments.json")
    if not os.path.exists(seg_path):
        raise HTTPException(status_code=400, detail="No saved transcript segments found. Re-run the full pipeline instead.")

    # Find source video (re-download if missing)
    source_files = glob.glob(os.path.join(job_dir, "source.*"))
    video_path = source_files[0] if source_files else None
    if not video_path or not os.path.exists(video_path):
        logger.info(f"Source video missing for {job_id}, re-downloading...")
        video_path = download_job_video(job)

    # Load segments
    with open(seg_path, "r") as f:
        segments = json.load(f)

    # Load diarization if available
    diar_path = os.path.join(job_dir, "diarization.json")
    diarized = None
    if os.path.exists(diar_path):
        with open(diar_path, "r") as f:
            diarized = json.load(f)

    # Clean old clips
    clips_dir = os.path.join(job_dir, "clips")
    if os.path.exists(clips_dir):
        shutil.rmtree(clips_dir)
    for clip in job.clips:
        clip.file_path = None
        clip.download_url = None
        clip.layout_mode_override = None
        clip.caption_style_override = None

    job.save()

    # Re-render
    try:
        render_job_clips(job, video_path, segments, diarized)
        job.status = "done"
        job.error = None
        logger.info(f"Re-render complete for job {job_id}")
    except Exception as e:
        logger.exception(f"Re-render failed for job {job_id}: {e}")
        job.status = "failed"
        job.error = str(e)

    job.save()
    html = render_template_str("partials/job_detail.html", job=job, api_token=API_TOKEN)
    return HTMLResponse(html)


@app.post("/jobs/{job_id}/clips/{clip_id}/meta")
async def update_clip_meta(
    job_id: str,
    clip_id: str,
    hook: Optional[str] = Form(None),
    title: Optional[str] = Form(None),
    _: str = Depends(verify_token),
):
    """Updates a clip's hook/title metadata without re-rendering the video."""
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    target = next((c for c in job.clips if c.id == clip_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Clip not found")

    changed = False
    if hook is not None:
        new_hook = hook.strip()
        if new_hook and new_hook != target.hook:
            target.hook = new_hook
            changed = True
    if title is not None:
        new_title = title.strip()
        if new_title and new_title != target.title:
            target.title = new_title
            changed = True

    if changed:
        job.save()
        logger.info(f"Updated metadata for clip {clip_id}")
    html = render_template_str("partials/job_detail.html", job=job, api_token=API_TOKEN)
    return HTMLResponse(html)


@app.post("/jobs/{job_id}/clips/{clip_id}/rerender")
async def rerender_one_clip(
    job_id: str,
    clip_id: str,
    layout_mode: Optional[str] = Form(None),
    caption_style: Optional[str] = Form(None),
    _: str = Depends(verify_token),
):
    """Re-renders a single clip, optionally with a different layout or caption style."""
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    target = next((c for c in job.clips if c.id == clip_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Clip not found")

    if caption_style and caption_style not in CAPTION_STYLES:
        raise HTTPException(status_code=400, detail=f"Invalid caption_style: {caption_style}")

    job_dir = job.get_dir()
    seg_path = os.path.join(job_dir, "segments.json")
    if not os.path.exists(seg_path):
        raise HTTPException(status_code=400, detail="No saved segments. Run full pipeline first.")

    # Locate source video; re-download if missing
    source_files = glob.glob(os.path.join(job_dir, "source.*"))
    video_path = source_files[0] if source_files else None
    if not video_path or not os.path.exists(video_path):
        logger.info(f"Source missing for {job_id}, re-downloading for per-clip rerender")
        video_path = download_job_video(job)

    with open(seg_path, "r") as f:
        segments = json.load(f)
    diar_path = os.path.join(job_dir, "diarization.json")
    diarized = None
    if os.path.exists(diar_path):
        with open(diar_path, "r") as f:
            diarized = json.load(f)

    # Drop the old rendered file so the new one replaces it cleanly
    if target.file_path and os.path.exists(target.file_path):
        try: os.remove(target.file_path)
        except Exception: pass

    # Attach job logger so progress entries appear in the SSE log stream
    log_path = os.path.join(job_dir, "pipeline.log")
    _write_log(log_path, "INFO", "render", f"Rerendering clip: {target.title}")

    try:
        # Build a progress callback that writes log entries
        def _progress(pct):
            _write_log(log_path, "INFO", "render", f"Rerender progress: {pct}%")

        render_one_clip(
            job=job, clip=target, video_path=video_path,
            segments=segments, diarized=diarized,
            override_layout=layout_mode if layout_mode and layout_mode != "auto" else None,
            override_caption_style=caption_style,
            progress_callback=_progress,
        )
        _write_log(log_path, "INFO", "render", f"Rerender complete for clip: {target.title}")
        # Persist per-clip overrides so selection survives page refresh
        target.layout_mode_override = layout_mode if layout_mode and layout_mode != "auto" else None
        target.caption_style_override = caption_style
        job.save()
        logger.info(f"Per-clip rerender complete: {clip_id} (layout={layout_mode}, style={caption_style})")
    except Exception as e:
        _write_log(log_path, "ERROR", "render", f"Rerender failed: {e}")
        logger.exception(f"Per-clip rerender failed for {clip_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Render failed: {e}")

    html = render_template_str("partials/job_detail.html", job=job, api_token=API_TOKEN)
    return HTMLResponse(html)


def _write_log(log_path: str, level: str, logger_name: str, msg: str):
    """Write a JSON log entry to the job's pipeline.log for SSE streaming."""
    import json, time
    try:
        entry = json.dumps({"ts": time.time(), "level": level, "logger": logger_name, "msg": msg}, ensure_ascii=False)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except Exception:
        pass


@app.post("/jobs/{job_id}/clips/{clip_id}/trim")
async def trim_clip(
    job_id: str,
    clip_id: str,
    trim_start: float = Form(...),
    trim_end: float = Form(...),
    _: str = Depends(verify_token)
):
    """Trims a clip to the given boundaries (relative to source video) and re-renders."""
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    target = None
    for clip in job.clips:
        if clip.id == clip_id:
            target = clip
            break
    if not target or not target.file_path or not os.path.exists(target.file_path):
        raise HTTPException(status_code=404, detail="Clip not found or not rendered")

    # Validate bounds
    if trim_end <= trim_start:
        raise HTTPException(status_code=400, detail="trim_end must be > trim_start")
    if trim_start < target.start:
        raise HTTPException(status_code=400, detail="trim_start cannot be before clip start")
    if trim_end > target.end:
        raise HTTPException(status_code=400, detail="trim_end cannot be after clip end")

    # Always cut from a pristine snapshot of the full render so trims are
    # non-destructive and can be re-widened later. Output-seek (-ss after -i)
    # with a re-encode is frame-accurate; the old stream-copy snapped to the
    # nearest keyframe and produced frozen/black starts and A/V drift.
    orig_path = target.file_path + ".orig"
    if not os.path.exists(orig_path):
        shutil.copy2(target.file_path, orig_path)

    rel_start = trim_start - target.start
    rel_duration = trim_end - trim_start
    tmp_path = target.file_path + ".trim.tmp.mp4"
    try:
        subprocess.run([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", orig_path,
            "-ss", f"{rel_start:.3f}",
            "-t", f"{rel_duration:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
            tmp_path
        ], check=True, capture_output=True, timeout=300)
        os.replace(tmp_path, target.file_path)
    except Exception as e:
        if os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except: pass
        raise HTTPException(status_code=500, detail=f"Trim failed: {e}")

    target.trim_start = trim_start
    target.trim_end = trim_end
    target.duration = round(rel_duration, 2)
    job.save()
    logger.info(f"Trimmed clip {clip_id} to [{trim_start:.1f}s – {trim_end:.1f}s]")
    html = render_template_str("partials/job_detail.html", job=job, api_token=API_TOKEN)
    return HTMLResponse(html)


@app.post("/jobs/{job_id}/clips/{clip_id}/favorite")
async def toggle_clip_favorite(job_id: str, clip_id: str, _: str = Depends(verify_token)):
    """Toggle the favorite/love state of a clip."""
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    target = next((c for c in job.clips if c.id == clip_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Clip not found")
    target.favorite = not target.favorite
    job.save()
    html = render_template_str("partials/job_detail.html", job=job, api_token=API_TOKEN)
    return HTMLResponse(html)


@app.post("/cleanup")
async def cleanup_old_jobs(days: int = 7, _: str = Depends(verify_token)):
    """Deletes jobs older than N days (0 = all jobs) to free disk space."""
    import time
    now = time.time()
    deleted = 0
    for job in Job.get_all():
        if days > 0:
            try:
                created = datetime.fromisoformat(job.created_at).timestamp()
            except Exception:
                continue
            if created >= now - (days * 86400):
                continue
        job_dir = job.get_dir()
        if os.path.exists(job_dir):
            shutil.rmtree(job_dir)
        deleted += 1
    label = "all" if days == 0 else f"older than {days} days"
    logger.info(f"Cleanup: deleted {deleted} job(s) {label}")
    return {"message": f"Deleted {deleted} job(s)"}

@app.post("/jobs/bulk-delete")
async def bulk_delete_jobs(request: Request, _: str = Depends(verify_token)):
    """Deletes multiple jobs by ID."""
    body = await request.json()
    job_ids = body.get("job_ids", [])
    if not job_ids:
        raise HTTPException(status_code=400, detail="No job_ids provided")
    deleted = 0
    for job_id in job_ids:
        job = Job.load(job_id)
        if job:
            job_dir = job.get_dir()
            if os.path.exists(job_dir):
                shutil.rmtree(job_dir)
            deleted += 1
    logger.info(f"Bulk delete: removed {deleted} job(s)")
    return {"message": f"Deleted {deleted} job(s)"}

@app.get("/jobs/{job_id}/clips/{clip_id}")
async def download_job_clip(job_id: str, clip_id: str, orig: int = 0, _: str = Depends(verify_token)):
    """Serves the rendered clip MP4 file. With ?orig=1, serves the pristine
    pre-trim render (used by the trim editor preview) when one exists."""
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Find matching clip
    target_clip = None
    for clip in job.clips:
        if clip.id == clip_id:
            target_clip = clip
            break

    if not target_clip or not target_clip.file_path or not os.path.exists(target_clip.file_path):
        raise HTTPException(status_code=404, detail="Clip video file not found or not rendered yet")

    path = target_clip.file_path
    if orig:
        orig_path = path + ".orig"
        if os.path.exists(orig_path):
            path = orig_path

    # Return file response
    return FileResponse(
        path=path,
        media_type="video/mp4",
        filename=f"{job_id}_{clip_id}.mp4"
    )


@app.get("/jobs/{job_id}/clips/{clip_id}/thumb")
async def get_clip_thumbnail(job_id: str, clip_id: str, _: str = Depends(verify_token)):
    """Serves the auto-generated JPG thumbnail for a clip."""
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    thumb_path = os.path.join(DATA_DIR, "jobs", job_id, "thumbnails", f"{clip_id}.jpg")
    if not os.path.exists(thumb_path):
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(path=thumb_path, media_type="image/jpeg")


# --- Studio (crop + subtitle + composition) ---

@app.get("/job/{job_id}/studio", response_class=HTMLResponse)
async def get_studio_page(job_id: str, request: Request, api_token: Optional[str] = Cookie(None)):
    if api_token != API_TOKEN:
        return templates.TemplateResponse(request, "login.html", {"error": None})
    job = Job.load(job_id)
    if not job:
        return RedirectResponse(url="/app", status_code=status.HTTP_303_SEE_OTHER)
    compositions = job.get_compositions()
    return templates.TemplateResponse(request, "studio.html", {
        "job": job, "api_token": API_TOKEN, "compositions": compositions,
    })

MIME_MAP = {
    ".mp4": "video/mp4", ".mov": "video/quicktime",
    ".webm": "video/webm", ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska", ".m4v": "video/mp4",
}

@app.get("/jobs/{job_id}/source")
async def get_source_video(job_id: str, _: str = Depends(verify_token)):
    """Serves the raw source video file for the clip editor."""
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job_dir = job.get_dir()
    source_files = glob.glob(os.path.join(job_dir, "source.*"))
    if not source_files:
        raise HTTPException(status_code=404, detail="Source video not found")
    ext = os.path.splitext(source_files[0])[1].lower()
    mime = MIME_MAP.get(ext, "video/mp4")
    return FileResponse(path=source_files[0], media_type=mime)

@app.get("/jobs/{job_id}/clips/{clip_id}/preview")
async def get_clip_preview(job_id: str, clip_id: str, _: str = Depends(verify_token)):
    """Serves the rendered clip file for the editor preview (trimmed to clip bounds)."""
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    target = next((c for c in job.clips if c.id == clip_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Clip not found")

    # Prefer the rendered clip file
    if target.file_path and os.path.exists(target.file_path):
        ext = os.path.splitext(target.file_path)[1].lower()
        mime = MIME_MAP.get(ext, "video/mp4")
        return FileResponse(path=target.file_path, media_type=mime)

    # Fallback: serve the source video
    job_dir = job.get_dir()
    source_files = glob.glob(os.path.join(job_dir, "source.*"))
    if not source_files:
        raise HTTPException(status_code=404, detail="Source video not found")
    ext = os.path.splitext(source_files[0])[1].lower()
    mime = MIME_MAP.get(ext, "video/mp4")
    return FileResponse(path=source_files[0], media_type=mime)

@app.get("/job/{job_id}/clips/{clip_id}/crop-frame")
async def get_crop_frame(job_id: str, clip_id: str, t: float = Query(2.5), _: str = Depends(verify_token)):
    """Serve a still frame from the source video at time `t` for the crop editor."""
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    target = next((c for c in job.clips if c.id == clip_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Clip not found")

    job_dir = job.get_dir()
    source_files = glob.glob(os.path.join(job_dir, "source.*"))
    if not source_files:
        raise HTTPException(status_code=404, detail="Source video not found")
    video_path = source_files[0]

    frame_time = max(target.start, min(t, target.end))
    try:
        proc = subprocess.run([
            "ffmpeg", "-y", "-ss", f"{frame_time:.3f}",
            "-i", video_path, "-vframes", "1",
            "-vf", "scale=720:-1", "-f", "image2pipe", "-",
        ], capture_output=True, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[:200])
        return Response(content=proc.stdout, media_type="image/jpeg")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to extract frame: {e}")

@app.post("/job/{job_id}/clips/{clip_id}/crop")
async def save_crop_overrides(
    job_id: str, clip_id: str,
    pan_x: float = Form(0), pan_y: float = Form(0), zoom: float = Form(1.0),
    _: str = Depends(verify_token),
):
    """Save crop overrides and re-render the clip."""
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    target = next((c for c in job.clips if c.id == clip_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Clip not found")

    target.crop_overrides = ClipCropOverrides(pan_x=pan_x, pan_y=pan_y, zoom=zoom)
    job.save()

    # Trigger re-render
    job_dir = job.get_dir()
    seg_path = os.path.join(job_dir, "segments.json")
    if not os.path.exists(seg_path):
        raise HTTPException(status_code=400, detail="No saved segments. Run full pipeline first.")
    source_files = glob.glob(os.path.join(job_dir, "source.*"))
    video_path = source_files[0] if source_files else None
    if not video_path or not os.path.exists(video_path):
        from app.pipeline.download import download_job_video
        video_path = download_job_video(job)

    with open(seg_path, "r") as f:
        segments = json.load(f)
    diar_path = os.path.join(job_dir, "diarization.json")
    diarized = None
    if os.path.exists(diar_path):
        with open(diar_path, "r") as f:
            diarized = json.load(f)

    if target.file_path and os.path.exists(target.file_path):
        try: os.remove(target.file_path)
        except Exception: pass

    try:
        render_one_clip(job=job, clip=target, video_path=video_path,
                        segments=segments, diarized=diarized)
        job.save()
        logger.info(f"Crop re-render complete: {clip_id}")
    except Exception as e:
        logger.exception(f"Crop re-render failed for {clip_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Render failed: {e}")

    html = render_template_str("partials/job_detail.html", job=job, api_token=API_TOKEN)
    return HTMLResponse(html)

@app.get("/job/{job_id}/clips/{clip_id}/subtitles")
async def get_clip_subtitles(job_id: str, clip_id: str, _: str = Depends(verify_token)):
    """Return subtitle segments for a clip as JSON."""
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    target = next((c for c in job.clips if c.id == clip_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Clip not found")

    seg_path = os.path.join(job.get_dir(), "segments.json")
    if not os.path.exists(seg_path):
        raise HTTPException(status_code=404, detail="No transcript segments found")

    with open(seg_path) as f:
        all_segments = json.load(f)

    # Filter segments overlapping this clip's time window
    clip_segments = []
    for i, seg in enumerate(all_segments):
        s, e = seg["start"], seg["end"]
        if s < target.end and e > target.start:
            clip_segments.append({
                "index": i,
                "original_index": i,
                "start": max(s, target.start),
                "end": min(e, target.end),
                "text": seg["text"],
                "edited": False,
            })

    # Apply any existing edits
    edited_map = {e.index: e for e in (target.subtitle_edits or [])}
    for seg in clip_segments:
        if seg["original_index"] in edited_map:
            ed = edited_map[seg["original_index"]]
            seg["text"] = ed.text
            seg["edited"] = True
            if ed.start_offset:
                seg["start"] += ed.start_offset
                seg["end"] += ed.start_offset

    return {
        "clip_id": clip_id,
        "duration": target.duration,
        "segments": clip_segments,
        "style": target.subtitle_style.model_dump() if target.subtitle_style else None,
    }

@app.post("/job/{job_id}/clips/{clip_id}/subtitles")
async def save_clip_subtitles(
    job_id: str, clip_id: str,
    request: Request, _: str = Depends(verify_token),
):
    """Save subtitle edits and re-render the clip."""
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    target = next((c for c in job.clips if c.id == clip_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Clip not found")

    body = await request.json()
    edits = body.get("edits", [])
    target.subtitle_edits = [ClipSubtitleEdit(**e) for e in edits]
    if "style" in body and body["style"]:
        target.subtitle_style = SubtitleStyleOverrides(**body["style"])
    job.save()

    # Trigger re-render
    job_dir = job.get_dir()
    seg_path = os.path.join(job_dir, "segments.json")
    if not os.path.exists(seg_path):
        raise HTTPException(status_code=400, detail="No saved segments.")
    source_files = glob.glob(os.path.join(job_dir, "source.*"))
    video_path = source_files[0] if source_files else None
    if not video_path or not os.path.exists(video_path):
        from app.pipeline.download import download_job_video
        video_path = download_job_video(job)

    with open(seg_path) as f:
        segments = json.load(f)
    diar_path = os.path.join(job_dir, "diarization.json")
    diarized = None
    if os.path.exists(diar_path):
        with open(diar_path) as f:
            diarized = json.load(f)

    if target.file_path and os.path.exists(target.file_path):
        try: os.remove(target.file_path)
        except Exception: pass

    try:
        render_one_clip(job=job, clip=target, video_path=video_path,
                        segments=segments, diarized=diarized)
        job.save()
        logger.info(f"Subtitle re-render complete: {clip_id}")
    except Exception as e:
        logger.exception(f"Subtitle re-render failed for {clip_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Render failed: {e}")

    return {"ok": True}


# --- Composition endpoints ---

@app.post("/jobs/{job_id}/compositions")
async def create_composition(job_id: str, title: str = Form("Untitled compilation"), _: str = Depends(verify_token)):
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    comp = Composition(
        id=str(uuid.uuid4()),
        job_id=job_id,
        title=title,
        clips=[CompositionClip(clip_id=c.id, order=i) for i, c in enumerate(job.clips) if c.download_url],
    )
    job.save_composition(comp)
    return comp.model_dump()

@app.get("/jobs/{job_id}/compositions")
async def list_compositions(job_id: str, _: str = Depends(verify_token)):
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return [c.model_dump() for c in job.get_compositions()]

@app.post("/jobs/{job_id}/compositions/{comp_id}/clips")
async def update_composition_clips(job_id: str, comp_id: str, request: Request, _: str = Depends(verify_token)):
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    comp = job.load_composition(comp_id)
    if not comp:
        raise HTTPException(status_code=404, detail="Composition not found")

    body = await request.json()
    clips_data = body.get("clips", [])
    comp.clips = [CompositionClip(**c) for c in clips_data]
    if "transition" in body:
        comp.transition = body["transition"]
    if "transition_duration" in body:
        comp.transition_duration = float(body["transition_duration"])
    if "title" in body:
        comp.title = body["title"]

    job.save_composition(comp)
    return comp.model_dump()

@app.post("/jobs/{job_id}/compositions/{comp_id}/render")
async def render_composition(job_id: str, comp_id: str, _: str = Depends(verify_token)):
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    comp = job.load_composition(comp_id)
    if not comp:
        raise HTTPException(status_code=404, detail="Composition not found")

    comp.status = "rendering"
    job.save_composition(comp)

    from app.pipeline.composer import render_composition as _render_comp
    try:
        output_path = _render_comp(comp, job)
        comp.status = "done"
        comp.file_path = output_path
        comp.download_url = f"/jobs/{job_id}/compositions/{comp_id}/download"
        job.save_composition(comp)
        logger.info(f"Composition rendered: {comp_id}")
        return {"status": "done", "download_url": comp.download_url}
    except Exception as e:
        logger.exception(f"Composition render failed: {e}")
        comp.status = "failed"
        job.save_composition(comp)
        raise HTTPException(status_code=500, detail=f"Render failed: {e}")

@app.get("/jobs/{job_id}/compositions/{comp_id}/download")
async def download_composition(job_id: str, comp_id: str, _: str = Depends(verify_token)):
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    comp = job.load_composition(comp_id)
    if not comp or not comp.file_path or not os.path.exists(comp.file_path):
        raise HTTPException(status_code=404, detail="Composition not found or not rendered")
    return FileResponse(path=comp.file_path, media_type="video/mp4",
                        filename=f"{comp.title.replace(' ', '_')}.mp4")

@app.delete("/jobs/{job_id}/compositions/{comp_id}")
async def delete_composition(job_id: str, comp_id: str, _: str = Depends(verify_token)):
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    comp = job.load_composition(comp_id)
    if not comp:
        raise HTTPException(status_code=404, detail="Composition not found")
    comp_path = os.path.join(job.get_dir(), "compositions", f"{comp_id}.json")
    if os.path.exists(comp_path):
        os.remove(comp_path)
    if comp.file_path and os.path.exists(comp.file_path):
        try: os.remove(comp.file_path)
        except: pass
    return {"ok": True}


# Job detail HTML page
# --- Per-clip editor ---

@app.get("/job/{job_id}/clip/{clip_id}/edit", response_class=HTMLResponse)
async def get_clip_editor(job_id: str, clip_id: str, request: Request, api_token: Optional[str] = Cookie(None)):
    if api_token != API_TOKEN:
        return templates.TemplateResponse(request, "login.html", {"error": None})
    job = Job.load(job_id)
    if not job:
        return RedirectResponse(url="/app", status_code=status.HTTP_303_SEE_OTHER)
    target = next((c for c in job.clips if c.id == clip_id), None)
    if not target:
        return RedirectResponse(url=f"/job/{job_id}", status_code=status.HTTP_303_SEE_OTHER)

    # Load segments for this clip (absolute timestamps from source)
    seg_path = os.path.join(job.get_dir(), "segments.json")
    clip_segments = []
    if os.path.exists(seg_path):
        with open(seg_path, "r") as f:
            all_segments = json.load(f)
        for i, seg in enumerate(all_segments):
            s, e = seg["start"], seg["end"]
            if s < target.end and e > target.start:
                clip_segments.append({
                    "index": i,
                    "start": max(s, target.start),  # absolute
                    "end": min(e, target.end),      # absolute
                    "text": seg["text"],
                })

    # Apply existing subtitle edits
    edited_map = {e.index: e for e in (target.subtitle_edits or [])}
    for seg in clip_segments:
        if seg["index"] in edited_map:
            seg["text"] = edited_map[seg["index"]].text
            seg["edited"] = True

    # Detect source video duration
    source_duration = 0
    source_files = glob.glob(os.path.join(job.get_dir(), "source.*"))
    if source_files:
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", source_files[0]],
                capture_output=True, text=True, timeout=30
            )
            source_duration = float(result.stdout.strip() or 0)
        except Exception:
            source_duration = target.end  # fallback

    return templates.TemplateResponse(request, "clip_editor.html", {
        "job": job, "clip": target, "api_token": API_TOKEN,
        "clip_segments": clip_segments, "source_duration": source_duration,
    })


@app.post("/jobs/{job_id}/clips/{clip_id}/apply")
async def apply_clip_edits(
    job_id: str, clip_id: str, request: Request,
    _: str = Depends(verify_token),
):
    """Save all clip edits (crop, subtitles, style, trim) and re-render once."""
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    target = next((c for c in job.clips if c.id == clip_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Clip not found")

    body = await request.json()

    # 1. Crop overrides
    crop = body.get("crop")
    if crop:
        target.crop_overrides = ClipCropOverrides(
            pan_x=float(crop.get("pan_x", 0)),
            pan_y=float(crop.get("pan_y", 0)),
            zoom=float(crop.get("zoom", 1.0)),
        )

    # 2. Subtitle edits
    edits = body.get("subtitle_edits", [])
    target.subtitle_edits = [ClipSubtitleEdit(**e) for e in edits]

    # 3. Subtitle style
    style = body.get("subtitle_style")
    if style:
        target.subtitle_style = SubtitleStyleOverrides(**style)

    # 4. Trim
    trim = body.get("trim")
    if trim:
        target.trim_start = float(trim.get("start", target.start))
        target.trim_end = float(trim.get("end", target.end))

    job.save()

    # Override layout / caption style
    override_layout = body.get("layout_mode")
    override_caption_style = body.get("caption_style")

    # Trigger re-render
    job_dir = job.get_dir()
    seg_path = os.path.join(job_dir, "segments.json")
    if not os.path.exists(seg_path):
        raise HTTPException(status_code=400, detail="No saved segments. Run full pipeline first.")
    source_files = glob.glob(os.path.join(job_dir, "source.*"))
    video_path = source_files[0] if source_files else None
    if not video_path or not os.path.exists(video_path):
        from app.pipeline.download import download_job_video
        video_path = download_job_video(job)

    with open(seg_path, "r") as f:
        segments = json.load(f)
    diar_path = os.path.join(job_dir, "diarization.json")
    diarized = None
    if os.path.exists(diar_path):
        with open(diar_path, "r") as f:
            diarized = json.load(f)

    if target.file_path and os.path.exists(target.file_path):
        try:
            os.remove(target.file_path)
        except Exception:
            pass

    log_path = os.path.join(job_dir, "pipeline.log")
    _write_log(log_path, "INFO", "render", f"Re-rendering clip: {target.title}")

    try:
        def _progress(pct):
            _write_log(log_path, "INFO", "render", f"Rerender progress: {pct}%")

        render_one_clip(
            job=job, clip=target, video_path=video_path,
            segments=segments, diarized=diarized,
            override_layout=override_layout if override_layout and override_layout != "auto" else None,
            override_caption_style=override_caption_style,
            progress_callback=_progress,
        )
        _write_log(log_path, "INFO", "render", f"Re-render complete for clip: {target.title}")
        target.layout_mode_override = override_layout if override_layout and override_layout != "auto" else None
        target.caption_style_override = override_caption_style
        job.save()
        logger.info(f"Apply edits re-render complete: {clip_id}")
        return {"ok": True, "download_url": target.download_url}
    except Exception as e:
        _write_log(log_path, "ERROR", "render", f"Re-render failed: {e}")
        logger.exception(f"Apply edits re-render failed for {clip_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Render failed: {e}")


@app.get("/job/{job_id}", response_class=HTMLResponse)
async def get_job_detail_page(job_id: str, request: Request, api_token: Optional[str] = Cookie(None)):
    """Renders the full job detail page (status + clips with video previews)."""
    if api_token != API_TOKEN:
        return templates.TemplateResponse(request, "login.html", {"error": None})
    job = Job.load(job_id)
    if not job:
        return RedirectResponse(url="/app", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "job_detail.html", {"job": job, "api_token": API_TOKEN})

UPLOAD_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

@app.post("/api/jobs/{job_id}/clips/{clip_id}/custom-thumbnail")
async def upload_custom_thumbnail(
    job_id: str,
    clip_id: str,
    file: UploadFile = File(...),
    _: str = Depends(verify_token),
):
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    target = next((c for c in job.clips if c.id == clip_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Clip not found")

    ext = os.path.splitext(file.filename or ".png")[1].lower()
    if ext not in UPLOAD_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported extension: {ext}")

    job_dir = job.get_dir()
    thumb_dir = os.path.join(job_dir, "custom_thumbnails")
    os.makedirs(thumb_dir, exist_ok=True)
    dest = os.path.join(thumb_dir, f"{clip_id}{ext}")

    contents = await file.read()
    with open(dest, "wb") as f:
        f.write(contents)

    # Remove any previously saved custom thumbnail for this clip
    if target.thumbnail_image_path and target.thumbnail_image_path != dest:
        try:
            os.remove(target.thumbnail_image_path)
        except Exception:
            pass

    target.thumbnail_image_path = dest
    job.save()

    log_path = os.path.join(job_dir, "pipeline.log")
    _write_log(log_path, "INFO", "render", f"Custom thumbnail set for clip: {target.title}")

    html = render_template_str("partials/job_detail.html", job=job, api_token=API_TOKEN)
    return HTMLResponse(html)


@app.get("/api/jobs/{job_id}/clips/{clip_id}/custom-thumbnail-image")
async def serve_custom_thumbnail(
    job_id: str,
    clip_id: str,
    _: str = Depends(verify_token),
):
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    target = next((c for c in job.clips if c.id == clip_id), None)
    if not target or not target.thumbnail_image_path:
        raise HTTPException(status_code=404, detail="No custom thumbnail")

    path = target.thumbnail_image_path
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Custom thumbnail file not found")

    ext = os.path.splitext(path)[1].lower()
    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(ext, "application/octet-stream")

    return FileResponse(path, media_type=media_type)


@app.delete("/api/jobs/{job_id}/clips/{clip_id}/custom-thumbnail")
async def remove_custom_thumbnail(
    job_id: str,
    clip_id: str,
    _: str = Depends(verify_token),
):
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    target = next((c for c in job.clips if c.id == clip_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Clip not found")

    if target.thumbnail_image_path:
        try:
            os.remove(target.thumbnail_image_path)
        except Exception:
            pass
        target.thumbnail_image_path = None
        job.save()

        job_dir = job.get_dir()
        log_path = os.path.join(job_dir, "pipeline.log")
        _write_log(log_path, "INFO", "render", f"Custom thumbnail removed for clip: {target.title}")

    html = render_template_str("partials/job_detail.html", job=job, api_token=API_TOKEN)
    return HTMLResponse(html)


# HTMX partial routes for dashboard
@app.get("/partials/jobs-list")
async def get_jobs_list_partial(request: Request, api_token: Optional[str] = Cookie(None)):
    """Renders just the jobs list for dynamic updates (self-terminating poll while active)."""
    if api_token != API_TOKEN:
        return HTMLResponse("Unauthorized", status_code=401)
    jobs = Job.get_all()
    has_active = any(j.status not in ("done", "failed") for j in jobs)
    return templates.TemplateResponse(
        request,
        "partials/jobs_list.html",
        {"jobs": jobs, "api_token": API_TOKEN, "has_active": has_active}
    )

@app.get("/partials/job/{job_id}")
async def get_job_detail_partial(job_id: str, request: Request, api_token: Optional[str] = Cookie(None)):
    """Renders just the job detail body for polling (status + clips)."""
    if api_token != API_TOKEN:
        return HTMLResponse("Unauthorized", status_code=401)
    job = Job.load(job_id)
    if not job:
        return HTMLResponse("Job not found", status_code=404)
    return templates.TemplateResponse(
        request,
        "partials/job_detail.html",
        {"job": job, "api_token": API_TOKEN}
    )
