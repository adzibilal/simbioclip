import os
import logging
import subprocess
from typing import List, Dict, Any
from openai import OpenAI
from app.settings_store import get_settings
from app.models import Job

logger = logging.getLogger("simbioclip.pipeline.transcribe")


def _stt_model_chain(settings) -> List[str]:
    """Ordered list of Whisper models to attempt: primary first, then fallback."""
    chain = []
    for m in (settings.stt_model, settings.stt_model_fallback):
        m = (m or "").strip()
        if m and m not in chain:
            chain.append(m)
    return chain or ["whisper-large-v3"]


def _transcribe_once(client: "OpenAI", audio_path: str, model: str, lang: str = None):
    """Run a single transcription request for one model. Falls back to a request
    without word/segment timestamp granularities if the router rejects them."""
    with open(audio_path, "rb") as f:
        transcribe_kwargs = {
            "model": model,
            "file": f,
            "response_format": "verbose_json",
            "timestamp_granularities": ["word", "segment"],
        }
        if lang:
            logger.info(f"Setting STT language parameter: {lang}")
            transcribe_kwargs["language"] = lang

        try:
            return client.audio.transcriptions.create(**transcribe_kwargs)
        except Exception as e:
            # Some routers don't support timestamp_granularities — retry without.
            logger.warning(f"Word-level STT not supported for '{model}', retrying without: {e}")
            transcribe_kwargs.pop("timestamp_granularities", None)
            f.seek(0)
            return client.audio.transcriptions.create(**transcribe_kwargs)

def extract_audio(video_path: str, output_audio_path: str) -> None:
    """
    Extracts a highly compressed, mono low-bitrate MP3 from video.
    This keeps transcription payloads small and fast.
    """
    logger.info(f"Extracting compressed audio from {video_path} to {output_audio_path}")
    
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn",                  # disable video
        "-acodec", "libmp3lame",
        "-ac", "1",             # mono
        "-ar", "16000",         # 16 kHz sample rate
        "-ab", "32k",           # 32 kbps bitrate
        output_audio_path
    ]
    
    try:
        # Run FFmpeg command and capture stderr in case of errors
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.decode() if e.stderr else "Unknown error"
        logger.error(f"FFmpeg audio extraction failed: {error_msg}")
        raise RuntimeError(f"Audio extraction failed: {error_msg}")

def transcribe_job_audio(job: Job, video_path: str) -> List[Dict[str, Any]]:
    """
    Handles audio extraction and transcription for a Job.
    Supports local faster-whisper (Mode B) and external router API (Mode A).
    """
    job_dir = job.get_dir()
    audio_path = os.path.join(job_dir, "audio.mp3")
    
    # Extract audio first
    extract_audio(video_path, audio_path)
    
    segments = []
    settings = get_settings()
    
    if settings.stt_mode == "router":
        logger.info(f"Using Mode A (STT API Router): {settings.stt_base_url}")
        if not settings.stt_base_url or not settings.stt_api_key:
            raise ValueError("STT_BASE_URL and STT_API_KEY must be set for STT_MODE='router'")

        client = OpenAI(base_url=settings.stt_base_url, api_key=settings.stt_api_key)

        model_chain = _stt_model_chain(settings)
        logger.info(f"STT model chain: {model_chain}")
        response = None
        last_error = None
        for idx, model in enumerate(model_chain):
            try:
                logger.info(f"Transcribing with model '{model}' ({idx + 1}/{len(model_chain)})...")
                response = _transcribe_once(client, audio_path, model, job.lang)
                break
            except Exception as e:
                last_error = e
                logger.warning(f"STT model '{model}' failed: {e}")

        if response is None:
            logger.error(f"All STT models failed ({model_chain}): {last_error}")
            raise RuntimeError(f"STT Router failed for all models {model_chain}: {last_error}")

        try:
            raw_data = None
            raw_segments = getattr(response, "segments", None)
            raw_words = getattr(response, "words", None)
            if raw_segments is None and isinstance(response, dict):
                raw_segments = response.get("segments", [])
                raw_words = response.get("words", [])
            elif raw_segments is None:
                import json
                try:
                    raw_data = json.loads(response.model_dump_json())
                    raw_segments = raw_data.get("segments", [])
                    raw_words = raw_data.get("words", [])
                except Exception:
                    raw_segments = []
                    raw_words = []
            raw_words = raw_words or []

            # Normalize words list
            words_list = []
            for w in raw_words:
                if isinstance(w, dict):
                    ws, we, wt = w.get("start", 0.0), w.get("end", 0.0), w.get("word", "")
                else:
                    ws, we, wt = getattr(w, "start", 0.0), getattr(w, "end", 0.0), getattr(w, "word", "")
                words_list.append({"start": float(ws), "end": float(we), "word": str(wt).strip()})

            for seg in raw_segments:
                if isinstance(seg, dict):
                    start = seg.get("start", 0.0)
                    end = seg.get("end", 0.0)
                    text = seg.get("text", "")
                else:
                    start = getattr(seg, "start", 0.0)
                    end = getattr(seg, "end", 0.0)
                    text = getattr(seg, "text", "")

                s, e = float(start), float(end)
                seg_words = [w for w in words_list if w["end"] > s - 0.05 and w["start"] < e + 0.05]
                segments.append({
                    "start": s,
                    "end": e,
                    "text": str(text).strip(),
                    "words": seg_words,
                })
                
        except Exception as e:
            logger.error(f"STT Router transcription failed: {e}")
            raise RuntimeError(f"STT Router failed: {e}")
            
    else:
        logger.info("Using Mode B (Local faster-whisper)")
        try:
            # We import faster_whisper inside the block to avoid overhead/errors if not needed
            from faster_whisper import WhisperModel
            
            # Use 'base' model for decent accuracy and low memory usage (~500MB)
            logger.info("Loading faster-whisper base model on CPU...")
            model = WhisperModel("base", device="cpu", compute_type="float32")
            
            logger.info("Running transcription...")
            # transcribe returns generator of segments
            transcribe_kwargs = {
                "beam_size": 5,
                "word_timestamps": True,
            }
            if job.lang:
                logger.info(f"Setting local STT language parameter: {job.lang}")
                transcribe_kwargs["language"] = job.lang

            raw_segments, info = model.transcribe(audio_path, **transcribe_kwargs)

            for seg in raw_segments:
                seg_words = []
                if getattr(seg, "words", None):
                    for w in seg.words:
                        seg_words.append({
                            "start": float(w.start),
                            "end": float(w.end),
                            "word": str(w.word).strip(),
                        })
                segments.append({
                    "start": float(seg.start),
                    "end": float(seg.end),
                    "text": seg.text.strip(),
                    "words": seg_words,
                })
                
        except Exception as e:
            logger.error(f"Local transcription failed: {e}")
            raise RuntimeError(f"Local transcription failed: {e}")
            
    # Clean up audio file after transcription to save disk space
    if os.path.exists(audio_path):
        try:
            os.remove(audio_path)
            logger.info("Cleaned up temporary audio.mp3 file.")
        except Exception as e:
            logger.warning(f"Failed to delete temporary audio: {e}")
            
    logger.info(f"Transcription complete. Got {len(segments)} segments.")
    return segments
