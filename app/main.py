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
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from redis import Redis
from rq import Queue

from app.config import API_TOKEN, REDIS_URL, DATA_DIR
from app.models import Job, Clip, PIPELINE_STEPS, CLIP_DURATION_PRESETS
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
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        # Set persistent token cookie (lasts for 30 days)
        response.set_cookie(key="api_token", value=API_TOKEN, max_age=30*24*60*60, httponly=True)
        return response
    
    return templates.TemplateResponse(request, "login.html", {"error": "Invalid Token"})

@app.get("/logout")
async def do_logout():
    """Logs out user by clearing the authentication cookie."""
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
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
            source_url
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if result.returncode != 0:
            raise RuntimeError(result.stderr[:200])
        data = json.loads(result.stdout)
        webpage_url = data.get("webpage_url", source_url)
        return {
            "title": data.get("title", "Unknown"),
            "duration": data.get("duration", 0),
            "thumbnail": data.get("thumbnail", ""),
            "webpage_url": webpage_url,
            "video_id": _extract_youtube_id(webpage_url),
        }
    except Exception as e:
        logger.error(f"Preview failed for {source_url}: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to fetch video info: {str(e)}")

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
              caption_style=caption_style, clip_duration=clip_duration, dense_cut=dense_cut)
    
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
    """Deletes a job metadata and all associated local files (clips/source)."""
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
        
    job_dir = job.get_dir()
    try:
        if os.path.exists(job_dir):
            shutil.rmtree(job_dir)
        logger.info(f"Successfully deleted files for job {job_id}")
        return {"message": f"Job {job_id} deleted successfully"}
    except Exception as e:
        logger.error(f"Failed to delete job files: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete job files: {e}")

@app.post("/jobs/{job_id}/retry")
async def retry_job(job_id: str, _: str = Depends(verify_token)):
    """Resets a failed job and re-enqueues it for processing."""
    job = Job.load(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    all_clips_empty = all(c.file_path is None for c in job.clips)
    if job.status not in ("failed", "done") or (job.status == "done" and not all_clips_empty):
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
    if clean_status not in ("done", "failed"):
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

    try:
        render_one_clip(
            job=job, clip=target, video_path=video_path,
            segments=segments, diarized=diarized,
            override_layout=layout_mode if layout_mode and layout_mode != "auto" else None,
            override_caption_style=caption_style,
        )
        job.save()
        logger.info(f"Per-clip rerender complete: {clip_id} (layout={layout_mode}, style={caption_style})")
    except Exception as e:
        logger.exception(f"Per-clip rerender failed for {clip_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Render failed: {e}")

    html = render_template_str("partials/job_detail.html", job=job, api_token=API_TOKEN)
    return HTMLResponse(html)


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

    target.trim_start = trim_start
    target.trim_end = trim_end

    # Re-cut the already rendered clip using stream copy (fast, no re-encode)
    rel_start = trim_start - target.start
    rel_duration = trim_end - trim_start
    tmp_path = target.file_path + ".trim.tmp"
    try:
        subprocess.run([
            "ffmpeg", "-y",
            "-ss", f"{rel_start:.3f}",
            "-i", target.file_path,
            "-t", f"{rel_duration:.3f}",
            "-c", "copy",
            tmp_path
        ], check=True, capture_output=True, timeout=300)
        os.replace(tmp_path, target.file_path)
    except Exception as e:
        if os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except: pass
        raise HTTPException(status_code=500, detail=f"Trim failed: {e}")

    job.save()
    logger.info(f"Trimmed clip {clip_id} to [{trim_start:.1f}s – {trim_end:.1f}s]")
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
async def download_job_clip(job_id: str, clip_id: str, _: str = Depends(verify_token)):
    """Serves the rendered clip MP4 file."""
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
        
    # Return file response
    return FileResponse(
        path=target_clip.file_path,
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


# Job detail HTML page
@app.get("/job/{job_id}", response_class=HTMLResponse)
async def get_job_detail_page(job_id: str, request: Request, api_token: Optional[str] = Cookie(None)):
    """Renders the full job detail page (status + clips with video previews)."""
    if api_token != API_TOKEN:
        return templates.TemplateResponse(request, "login.html", {"error": None})
    job = Job.load(job_id)
    if not job:
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "job_detail.html", {"job": job, "api_token": API_TOKEN})

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
