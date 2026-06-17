import os
import json
import glob
import shutil
import logging
from app.models import Job, PIPELINE_STEPS
from app.pipeline.download import download_job_video
from app.pipeline.transcribe import transcribe_job_audio
from app.pipeline.clean_transcript import clean_segments
from app.pipeline.moments import detect_moments
from app.pipeline.scene_classifier import classify_content_type
from app.pipeline.diarization import diarize_speakers
from app.pipeline.render import render_job_clips

logger = logging.getLogger("simbioclip.pipeline.orchestrator")

# When a step is retried, it and every step that depends on it must be recomputed.
# render depends on moments+classify+diarize; those depend on transcribe; transcribe
# depends on download. (moments/classify/diarize are independent siblings.)
_STEP_INVALIDATES = {
    "download":   ["download", "transcribe", "moments", "classify", "diarize", "render"],
    "transcribe": ["transcribe", "moments", "classify", "diarize", "render"],
    "moments":    ["moments", "render"],
    "classify":   ["classify", "render"],
    "diarize":    ["diarize", "render"],
    "render":     ["render"],
}


def _find_existing_source(job_dir: str) -> str | None:
    """Return the path of an already-downloaded source video, if any."""
    for f in glob.glob(os.path.join(job_dir, "source.*")):
        if f.endswith((".part", ".ytdl", ".temp")):
            continue
        if f.endswith(".mp4"):
            return f
    # Fall back to any non-temp source.* file
    for f in glob.glob(os.path.join(job_dir, "source.*")):
        if not f.endswith((".part", ".ytdl", ".temp")):
            return f
    return None


def _safe_remove(path: str) -> None:
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError as e:
        logger.warning(f"Could not remove {path}: {e}")


def reset_job_step(job: Job, step: str) -> set:
    """Clear the on-disk and in-memory artifacts for `step` and every step that
    depends on it, then mark the job queued so the orchestrator recomputes from
    there on the next run. Cached artifacts of earlier steps are kept so they are
    reused. Returns the set of invalidated step ids.
    """
    job_dir = job.get_dir()
    invalid = set(_STEP_INVALIDATES.get(step, [step]))

    if "download" in invalid:
        for f in glob.glob(os.path.join(job_dir, "source.*")):
            _safe_remove(f)
        job.download_pct = None
        job.download_downloaded_mb = None
        job.download_total_mb = None
    if "transcribe" in invalid:
        _safe_remove(os.path.join(job_dir, "segments_raw.json"))
        _safe_remove(os.path.join(job_dir, "segments.json"))
        job.silence_ranges = []
    if "moments" in invalid:
        job.clips = []
    if "classify" in invalid:
        job.content_type = None
    if "diarize" in invalid:
        _safe_remove(os.path.join(job_dir, "diarization.json"))
        job.speaker_count = None
    if "render" in invalid:
        for sub in ("clips", "thumbnails"):
            d = os.path.join(job_dir, sub)
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
        for c in job.clips:
            c.file_path = None
            c.download_url = None
            c.thumbnail_url = None

    job.error = None
    job.failed_step = None
    job.status = "queued"
    job.save()
    logger.info(f"Reset job {job.id} from step '{step}' (invalidated: {sorted(invalid)})")
    return invalid


def process_video_job(job_id: str) -> None:
    job = Job.load(job_id)
    if not job:
        logger.error(f"Cannot run job orchestrator. Job {job_id} not found.")
        return

    logger.info(f"Starting orchestration pipeline for job {job_id}...")

    current_step = "download"
    try:
        job_dir = job.get_dir()

        # --- Step: Download (reuse source.* if already on disk) ---
        current_step = "download"
        video_path = _find_existing_source(job_dir)
        if video_path:
            logger.info(f"Reusing existing source: {video_path}")
        else:
            job.status = "downloading"
            job.save()
            video_path = download_job_video(job)

        # --- Step: Transcribe (reuse segments_raw.json if already on disk) ---
        current_step = "transcribe"
        raw_path = os.path.join(job_dir, "segments_raw.json")
        if os.path.exists(raw_path):
            logger.info(f"Reusing cached transcript: {raw_path}")
            with open(raw_path, "r", encoding="utf-8") as f:
                raw_segments = json.load(f)
        else:
            job.status = "transcribing"
            job.save()
            raw_segments = transcribe_job_audio(job, video_path)
            with open(raw_path, "w", encoding="utf-8") as f:
                json.dump(raw_segments, f, indent=2)

        # Cleanup pass is cheap and deterministic — always run so changes ship without re-transcribing.
        segments, silence_ranges = clean_segments(raw_segments, job.lang)
        job.silence_ranges = silence_ranges
        seg_path = os.path.join(job_dir, "segments.json")
        with open(seg_path, "w", encoding="utf-8") as f:
            json.dump(segments, f, indent=2)
        job.save()

        # --- Step: Moments (reuse job.clips if already detected) ---
        current_step = "moments"
        if job.clips:
            logger.info(f"Reusing {len(job.clips)} previously detected moment(s)")
        else:
            job.status = "finding_moments"
            job.save()
            clips = detect_moments(job, segments)
            if not clips:
                raise RuntimeError("No engaging moments were found in the transcript.")
            job.clips = clips
            job.save()

        # --- Step: Content classification (reuse job.content_type if already set) ---
        current_step = "classify"
        if job.layout_mode != "auto":
            job.content_type = job.layout_mode
            logger.info(f"Using user-selected layout: {job.layout_mode}")
            job.save()
        elif job.content_type:
            logger.info(f"Reusing cached classification: {job.content_type}")
        else:
            job.status = "classifying"
            job.save()
            job.content_type = classify_content_type(video_path, segments)
            logger.info(f"Content classified as: {job.content_type}")
            job.save()

        # --- Step: Diarization (reuse diarization.json if already on disk) ---
        current_step = "diarize"
        diar_path = os.path.join(job_dir, "diarization.json")
        if os.path.exists(diar_path):
            logger.info(f"Reusing cached diarization: {diar_path}")
            with open(diar_path, "r", encoding="utf-8") as f:
                diarized = json.load(f)
        else:
            job.status = "diarizing"
            job.save()
            diarized = diarize_speakers(segments, job.lang)
            if diarized:
                with open(diar_path, "w", encoding="utf-8") as f:
                    json.dump(diarized, f, indent=2)

        if diarized:
            speakers = set(s["speaker"] for s in diarized)
            job.speaker_count = len(speakers)
            job.save()
            logger.info(f"Diarization: {job.speaker_count} speaker(s) detected")

        # --- Step: Render (skip clips already rendered) ---
        current_step = "render"
        unrendered = [c for c in job.clips if not (c.file_path and os.path.exists(c.file_path))]
        if unrendered:
            render_job_clips(job, video_path, segments, diarized)
        else:
            logger.info("All clips already rendered; skipping render step.")

        job.status = "done"
        job.failed_step = None
        logger.info(f"Pipeline finished successfully for job {job_id}.")

    except Exception as e:
        logger.exception(f"Pipeline failed for job {job_id} at step '{current_step}': {e}")
        job.status = "failed"
        job.error = str(e)
        job.failed_step = current_step

    finally:
        # Keep source/segments/diarization on disk so retry can resume mid-pipeline
        # without re-downloading or re-transcribing.
        job.save()
