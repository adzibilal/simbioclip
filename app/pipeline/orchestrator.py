import os
import json
import glob
import shutil
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    "moments":    ["moments", "classify", "diarize", "render"],
    "classify":   ["classify", "moments", "diarize", "render"],
    "diarize":    ["diarize", "moments", "classify", "render"],
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
    # Remove cancellation marker so retries can proceed
    cancel_file = os.path.join(job_dir, ".cancelled")
    if os.path.exists(cancel_file):
        try:
            os.remove(cancel_file)
        except OSError:
            pass
    job.save()
    logger.info(f"Reset job {job.id} from step '{step}' (invalidated: {sorted(invalid)})")
    return invalid


def _job_is_cancelled(job_dir: str) -> bool:
    return os.path.exists(os.path.join(job_dir, ".cancelled"))


def _raise_if_cancelled(job: Job, job_dir: str):
    if _job_is_cancelled(job_dir):
        job.status = "cancelled"
        job.error = "Cancelled by user"
        job.save()
        logger.info(f"Job {job.id} was cancelled. Aborting pipeline.")
        raise SystemExit(0)


def process_video_job(job_id: str) -> None:
    job = Job.load(job_id)
    if not job:
        logger.error(f"Cannot run job orchestrator. Job {job_id} not found.")
        return

    logger.info(f"Starting orchestration pipeline for job {job_id}...")

    current_step = "download"
    try:
        job_dir = job.get_dir()
        _raise_if_cancelled(job, job_dir)

        # --- Step: Download (reuse source.* if already on disk) ---
        current_step = "download"
        video_path = _find_existing_source(job_dir)
        if video_path:
            logger.info(f"Reusing existing source: {video_path}")
        else:
            job.status = "downloading"
            job.save()
            _raise_if_cancelled(job, job_dir)
            video_path = download_job_video(job)
            _raise_if_cancelled(job, job_dir)

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

        # --- Step: Moments, Content classification, Diarization (parallel) ---
        # These three are independent siblings — run them concurrently.
        # Results are saved immediately per-step so the UI updates individually.
        needs_moments = not job.clips
        needs_classify = (
            job.layout_mode == "auto"
            and not job.content_type
        )
        diar_path = os.path.join(job_dir, "diarization.json")
        needs_diarize = not os.path.exists(diar_path)

        _STATUS_MAP = {"moments": "finding_moments", "classify": "classifying", "diarize": "diarizing"}
        pending_parallel = set()
        if needs_moments: pending_parallel.add("moments")
        if needs_classify: pending_parallel.add("classify")
        if needs_diarize: pending_parallel.add("diarize")

        if pending_parallel:
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {}

                if needs_moments:
                    job.status = "finding_moments"
                    job.save()
                    futures["moments"] = pool.submit(detect_moments, job, segments)

                if needs_classify:
                    job.status = "classifying"
                    job.save()
                    futures["classify"] = pool.submit(classify_content_type, video_path, segments)

                if needs_diarize:
                    job.status = "diarizing"
                    job.save()
                    futures["diarize"] = pool.submit(diarize_speakers, segments, job.lang)

                future_to_name = {v: k for k, v in futures.items()}
                for future in as_completed(future_to_name):
                    name = future_to_name[future]
                    current_step = name
                    try:
                        result = future.result()
                        pending_parallel.discard(name)

                        # Save individual artifact immediately so UI updates per-step
                        if name == "classify":
                            job.content_type = result if result else "unknown"
                            logger.info(f"Content classified as: {job.content_type}")
                        elif name == "diarize":
                            if result:
                                with open(diar_path, "w", encoding="utf-8") as f:
                                    json.dump(result, f, indent=2)
                        elif name == "moments":
                            if not result:
                                job.save()
                                raise RuntimeError("No engaging moments were found in the transcript.")
                            job.clips = result

                        # Keep job.status as one of the still-running steps so the
                        # pipeline_steps() method shows remaining as "running".
                        remaining = sorted(pending_parallel)
                        job.status = _STATUS_MAP[remaining[0]] if remaining else "moments_complete"
                        job.save()
                    except Exception as e:
                        # Persist whatever was already saved before re-raising
                        if job.content_type or os.path.exists(diar_path) or job.clips:
                            job.save()
                        raise RuntimeError(f"{name} failed: {e}")

        # Post parallel-phase: apply user layout / load cached artifacts
        if not needs_classify:
            if job.layout_mode != "auto":
                job.content_type = job.layout_mode
                logger.info(f"Using user-selected layout: {job.layout_mode}")
            elif not job.content_type:
                job.content_type = "unknown"
                logger.info(f"Using cached classification: {job.content_type}")
            else:
                logger.info(f"Reusing cached classification: {job.content_type}")

        current_step = "diarize"
        diarized = None
        if os.path.exists(diar_path):
            with open(diar_path, "r", encoding="utf-8") as f:
                diarized = json.load(f)
            logger.info(f"Reusing cached diarization: {diar_path}")

        if diarized:
            speakers = set(s["speaker"] for s in diarized)
            job.speaker_count = len(speakers)
            logger.info(f"Diarization: {job.speaker_count} speaker(s) detected")

        if not needs_moments:
            logger.info(f"Reusing {len(job.clips)} previously detected moment(s)")

        job.save()

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

    except SystemExit:
        pass
    except Exception as e:
        logger.exception(f"Pipeline failed for job {job_id} at step '{current_step}': {e}")
        job.status = "failed"
        job.error = str(e)
        job.failed_step = current_step

    finally:
        # Keep source/segments/diarization on disk so retry can resume mid-pipeline
        # without re-downloading or re-transcribing.
        job.save()
