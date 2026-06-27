import os
import glob
import time
import logging
import subprocess
import threading
import yt_dlp
from app.models import Job
from app.settings_store import get_settings

logger = logging.getLogger("simbioclip.pipeline.download")


def _refresh_cookies():
    """Re-extract cookies to COOKIES_FILE using Chrome's cookie database.

    Requires the user to have run ``python manage.py auth`` at least once so that
    a Chrome profile exists at /app/data/chrome-profile with active YouTube login.
    After that, this function can re-export cookies without further manual steps.
    """
    chrome_profile = "/app/data/chrome-profile"
    cookie_db = os.path.join(chrome_profile, "Default", "Cookies")
    cookies_file = get_settings().cookies_file
    if not os.path.exists(cookie_db) or not cookies_file:
        logger.info("Cookie refresh skipped: no Chrome profile or COOKIES_FILE not set")
        return
    try:
        args = [
            "yt-dlp",
            "--cookies-from-browser", f"chrome:{chrome_profile}",
            "--cookies", cookies_file,
            "--skip-download",
            "--quiet",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ]
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            logger.info(f"Refreshed cookies from Chrome profile ({chrome_profile})")
        else:
            logger.warning(f"Cookie refresh failed: {result.stderr[:200]}")
    except Exception as e:
        logger.warning(f"Cookie refresh error: {e}")


def _start_size_watcher(job: Job, job_dir: str, total_bytes: int, stop_event: threading.Event):
    """Poll the source.* partial file size and surface progress to job.status + logs.
    Also aborts early if a .cancelled marker file appears.
    """

    def watch():
        last_log_t = 0.0
        last_save_t = 0.0
        last_pct = -100.0
        while not stop_event.is_set():
            if os.path.exists(os.path.join(job_dir, ".cancelled")):
                logger.info(f"Job {job.id} cancelled during download — stopping watcher.")
                break
            try:
                files = glob.glob(os.path.join(job_dir, "source.*"))
                downloaded = sum(os.path.getsize(f) for f in files if os.path.isfile(f))
                now = time.time()

                mb = downloaded / (1024 * 1024)
                if total_bytes > 0:
                    pct = min(99.0, (downloaded / total_bytes) * 100.0)
                    total_mb = total_bytes / (1024 * 1024)
                    pct_jump = pct - last_pct >= 1
                    if pct_jump or (now - last_log_t) >= 15:
                        last_log_t = now
                        logger.info(
                            f"Download progress: {pct:.1f}% ({mb:.1f}/{total_mb:.1f} MB)"
                        )
                    if pct_jump or (now - last_save_t) >= 2:
                        last_save_t = now
                        last_pct = pct
                        job.status = f"downloading {pct:.0f}%"
                        job.download_pct = round(pct, 1)
                        job.download_downloaded_mb = round(mb, 1)
                        job.download_total_mb = round(total_mb, 1)
                        try:
                            job.save()
                        except Exception as ex:
                            logger.warning(f"Failed to persist download progress: {ex}")
                else:
                    if (now - last_log_t) >= 15:
                        last_log_t = now
                        logger.info(f"Download progress: {mb:.1f} MB downloaded")
                    if (now - last_save_t) >= 2:
                        last_save_t = now
                        job.download_pct = None
                        job.download_downloaded_mb = round(mb, 1)
                        job.download_total_mb = None
                        try:
                            job.save()
                        except Exception as ex:
                            logger.warning(f"Failed to persist download progress: {ex}")
            except Exception as ex:
                logger.warning(f"Size watcher error: {ex}")
            stop_event.wait(1.0)

    t = threading.Thread(target=watch, daemon=True)
    t.start()
    return t


def _estimate_total_bytes(source_url: str, ydl_opts: dict, clip_start, clip_end) -> int:
    """Probe total download size using yt-dlp's info extraction (no download)."""
    cookies_file = get_settings().cookies_file
    probe_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": ydl_opts.get("format"),
        "skip_download": True,
        "js_runtimes": {"node": {}},
    }
    if cookies_file and os.path.exists(cookies_file):
        with open(cookies_file) as _f:
            _content = _f.read()
        if any(_line.strip() and '\t' in _line for _line in _content.splitlines()):
            probe_opts["cookiefile"] = cookies_file
    try:
        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(source_url, download=False)
        duration = info.get("duration") or 0
        requested = info.get("requested_formats") or [info]
        total = 0
        for fmt in requested:
            sz = fmt.get("filesize") or fmt.get("filesize_approx") or 0
            if sz:
                total += sz
            else:
                tbr = fmt.get("tbr") or 0
                if tbr and duration:
                    total += int(tbr * 1000 / 8 * duration)
        if total and clip_start is not None and clip_end is not None and duration:
            ratio = max(0.0, min(1.0, (clip_end - clip_start) / duration))
            total = int(total * ratio)
        return total
    except Exception as ex:
        logger.warning(f"Could not estimate download size: {ex}")
        return 0


def _resolve_format(resolution: str) -> str:
    height_map = {
        "2160p": 2160, "1440p": 1440, "1080p": 1080,
        "720p": 720, "480p": 480, "360p": 360,
    }
    h = height_map.get(resolution)
    if h is None:
        return "bestvideo+bestaudio/best"
    return (
        f"bestvideo[height<={h}]+bestaudio/"
        f"best[height<={h}]/best"
    )


def _capture_channel_info(job: Job, source_url: str) -> None:
    """Extract channel name/URL from yt-dlp metadata and persist on the job."""
    try:
        probe_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
            "js_runtimes": {"node": {}},
        }
        cf = get_settings().cookies_file
        if cf and os.path.exists(cf):
            with open(cf) as _f:
                _c = _f.read()
            if any(_l.strip() and '\t' in _l for _l in _c.splitlines()):
                probe_opts["cookiefile"] = cf
        with yt_dlp.YoutubeDL(probe_opts) as ydl:
            info = ydl.extract_info(source_url, download=False)
        channel = info.get("channel") or info.get("uploader") or ""
        channel_url = info.get("channel_url") or info.get("uploader_url") or ""
        if channel:
            job.channel_name = channel
            job.channel_url = channel_url
            job.save()
            logger.info(f"Channel info: {channel} ({channel_url})")
    except Exception as e:
        logger.warning(f"Could not extract channel info: {e}")


def download_job_video(job: Job) -> str:
    job_dir = job.get_dir()
    settings = get_settings()

    if job.source_url:
        logger.info(f"Starting download for URL: {job.source_url}")

        _capture_channel_info(job, job.source_url)

        ydl_opts = {
            "format": _resolve_format(job.download_resolution),
            "outtmpl": os.path.join(job_dir, "source.%(ext)s"),
            "merge_output_format": "mp4",
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "http_chunk_size": 10 * 1024 * 1024,
            "continuedl": True,
            "retries": 20,
            "fragment_retries": 20,
            "file_access_retries": 5,
            "extractor_retries": 3,
            "socket_timeout": 60,
            "concurrent_fragment_downloads": settings.concurrent_fragments,
            "throttled_rate": settings.throttled_rate,
            "js_runtimes": {"node": {}},
            "retry_sleep_functions": {
                "http": lambda n: min(2 ** n, 30),
                "fragment": lambda n: min(2 ** n, 30),
            },
        }

        if settings.cookies_file and os.path.exists(settings.cookies_file):
            with open(settings.cookies_file) as f:
                content = f.read()
            if any(line.strip() and '\t' in line for line in content.splitlines()):
                ydl_opts["cookiefile"] = settings.cookies_file
                logger.info(f"Using cookies file: {settings.cookies_file}")
        # Browser-derived cookies are extracted to COOKIES_FILE by manage.py auth.
        # Direct cookiesfrombrowser is not used here because Chrome's persistent
        # profile doesn't survive container restarts; the extracted flat file does.

        if job.clip_start is not None and job.clip_end is not None and job.clip_end > job.clip_start:
            logger.info(f"Downloading range [{job.clip_start}s – {job.clip_end}s]")
            ydl_opts["download_ranges"] = lambda info, ydl: [
                {"start_time": job.clip_start, "end_time": job.clip_end}
            ]
            ydl_opts["force_keyframes_at_cuts"] = True

        has_range = "download_ranges" in ydl_opts

        # yt-dlp native downloader with high concurrency for fragment DASH
        total_bytes = _estimate_total_bytes(job.source_url, ydl_opts, job.clip_start, job.clip_end)
        if total_bytes:
            logger.info(f"Estimated total download size: {total_bytes / (1024*1024):.1f} MB")
        stop_event = threading.Event()
        _start_size_watcher(job, job_dir, total_bytes, stop_event)
        try:
            if os.path.exists(os.path.join(job_dir, ".cancelled")):
                raise RuntimeError("Job cancelled before download started")
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([job.source_url])
        except Exception as e:
            if os.path.exists(os.path.join(job_dir, ".cancelled")):
                logger.info(f"Download aborted for cancelled job {job.id}")
                raise RuntimeError("Job was cancelled")
            err_msg = str(e)
            # Retry once with fresh cookies if auth-related failure
            if "Sign in" in err_msg:
                logger.info("Auth failed — refreshing cookies and retrying once...")
                _refresh_cookies()
                if "cookiefile" not in ydl_opts and settings.cookies_file:
                    ydl_opts["cookiefile"] = settings.cookies_file
                try:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([job.source_url])
                    logger.info("Second attempt succeeded after cookie refresh")
                    return _resolve_source_file(job_dir)
                except Exception as e2:
                    logger.error(f"Retry also failed: {e2}")
                    raise RuntimeError(f"Video download failed: {str(e2)}")
            logger.error(f"yt-dlp failed: {e}")
            raise RuntimeError(f"Video download failed: {err_msg}")
        finally:
            stop_event.set()

    return _resolve_source_file(job_dir)


def _resolve_source_file(job_dir: str) -> str:
    files = glob.glob(os.path.join(job_dir, "source.*"))
    if not files:
        raise FileNotFoundError("Source video file not found in job directory.")

    for f in files:
        if f.endswith(".mp4"):
            logger.info(f"Found source video: {f}")
            return f

    logger.info(f"Found source video (non-mp4): {files[0]}")
    return files[0]
