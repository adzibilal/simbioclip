import os
import glob
import time
import logging
import threading
import yt_dlp
from app.models import Job
from app.config import COOKIES_FILE

logger = logging.getLogger("simbioclip.pipeline.download")


def _start_size_watcher(job: Job, job_dir: str, total_bytes: int, stop_event: threading.Event):
    """Poll the source.* partial file size and surface progress to job.status + logs.

    Used because yt-dlp's progress_hooks only fire on `finished` when
    force_keyframes_at_cuts=True triggers the external (ffmpeg) downloader.
    """

    def watch():
        last_log_t = 0.0
        last_save_t = 0.0
        last_pct = -100.0
        while not stop_event.is_set():
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
    probe_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": ydl_opts.get("format"),
        "skip_download": True,
        "js_runtimes": {"node": {}},
    }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        with open(COOKIES_FILE) as _f:
            _content = _f.read()
        if any(_line.strip() and '\t' in _line for _line in _content.splitlines()):
            probe_opts["cookiefile"] = COOKIES_FILE
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


def download_job_video(job: Job) -> str:
    job_dir = job.get_dir()

    if job.source_url:
        logger.info(f"Starting download for URL: {job.source_url}")

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
            "concurrent_fragment_downloads": 1,
            "js_runtimes": {"node": {}},
            "retry_sleep_functions": {
                "http": lambda n: min(2 ** n, 30),
                "fragment": lambda n: min(2 ** n, 30),
            },
        }

        if COOKIES_FILE and os.path.exists(COOKIES_FILE):
            with open(COOKIES_FILE) as f:
                content = f.read()
            if any(line.strip() and '\t' in line for line in content.splitlines()):
                ydl_opts["cookiefile"] = COOKIES_FILE
                logger.info(f"Using cookies file: {COOKIES_FILE}")

        if job.clip_start is not None and job.clip_end is not None and job.clip_end > job.clip_start:
            logger.info(f"Downloading range [{job.clip_start}s – {job.clip_end}s]")
            ydl_opts["download_ranges"] = lambda info, ydl: [
                {"start_time": job.clip_start, "end_time": job.clip_end}
            ]
            ydl_opts["force_keyframes_at_cuts"] = True

        total_bytes = _estimate_total_bytes(job.source_url, ydl_opts, job.clip_start, job.clip_end)
        if total_bytes:
            logger.info(f"Estimated total download size: {total_bytes / (1024*1024):.1f} MB")
        stop_event = threading.Event()
        _start_size_watcher(job, job_dir, total_bytes, stop_event)
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([job.source_url])
        except Exception as e:
            logger.error(f"yt-dlp failed: {e}")
            raise RuntimeError(f"Video download failed: {str(e)}")
        finally:
            stop_event.set()

    files = glob.glob(os.path.join(job_dir, "source.*"))
    if not files:
        raise FileNotFoundError("Source video file not found in job directory.")

    for f in files:
        if f.endswith(".mp4"):
            logger.info(f"Found source video: {f}")
            return f

    logger.info(f"Found source video (non-mp4): {files[0]}")
    return files[0]
