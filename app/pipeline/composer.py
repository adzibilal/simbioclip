import os
import json
import subprocess
import tempfile
import logging
from typing import List, Optional
from app.models import Job, Composition, CompositionClip

logger = logging.getLogger("simbioclip.pipeline.composer")


def render_composition(comp: Composition, job: Job) -> str:
    """Render a composition: concatenate clips with optional transitions."""
    temp_dir = tempfile.mkdtemp(prefix="simbioclip_comp_")
    trimmed_files: List[str] = []

    try:
        for cc in comp.clips:
            clip = next((c for c in job.clips if c.id == cc.clip_id), None)
            if not clip or not clip.file_path or not os.path.exists(clip.file_path):
                logger.warning(f"Clip {cc.clip_id} not found or not rendered, skipping")
                continue

            trim_s = cc.trim_start or 0
            trim_e = cc.trim_end or clip.duration
            if trim_e <= trim_s:
                continue

            trimmed = os.path.join(temp_dir, f"{cc.order:04d}.mp4")
            subprocess.run([
                "ffmpeg", "-y",
                "-ss", f"{trim_s:.3f}",
                "-i", clip.file_path,
                "-t", f"{(trim_e - trim_s):.3f}",
                "-c", "copy",
                trimmed,
            ], check=True, capture_output=True, timeout=300)
            trimmed_files.append(trimmed)

        if not trimmed_files:
            raise RuntimeError("No clips to render")

        comp_dir = os.path.join(job.get_dir(), "compositions")
        os.makedirs(comp_dir, exist_ok=True)
        output_path = os.path.join(comp_dir, f"{comp.id}.mp4")

        if comp.transition == "crossfade" and len(trimmed_files) >= 2:
            _render_crossfade(trimmed_files, output_path, comp.transition_duration)
        else:
            _render_concat(trimmed_files, output_path)

        return output_path

    finally:
        for f in trimmed_files:
            try: os.remove(f)
            except: pass
        try: os.rmdir(temp_dir)
        except: pass


def _render_concat(files: List[str], output: str):
    """Fast concatenation using concat demuxer (no re-encode)."""
    concat_file = os.path.join(os.path.dirname(files[0]), "concat.txt")
    with open(concat_file, "w") as f:
        for path in files:
            f.write(f"file '{path}'\n")

    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", concat_file, "-c", "copy", output,
    ], check=True, capture_output=True, timeout=600)

    try: os.remove(concat_file)
    except: pass


def _render_crossfade(files: List[str], output: str, duration: float = 0.5):
    """Concatenate with crossfade between clips (requires re-encode)."""
    inputs = []
    filter_parts = []
    prev_label = None
    audio_filters = []

    for i, path in enumerate(files):
        label = f"c{i}"
        inputs.extend(["-i", path])

        if i == 0:
            filter_parts.append(f"[{label}:v]setpts=PTS-STARTPTS[v{i}]")
            audio_filters.append(f"[{label}:a]asetpts=PTS-STARTPTS[a{i}]")
            prev_label = i
        else:
            cf = f"[v{prev_label}][v{i}]xfade=transition=fade:duration={duration:.2f}:offset=offset{i}[v{i}]"
            # We need to compute offset = sum of previous clip durations minus crossfade
            filter_parts.append(f"[{label}:v]setpts=PTS-STARTPTS[v{i}_raw]")
            filter_parts.append(cf)
            audio_filters.append(f"[{label}:a]asetpts=PTS-STARTPTS[a{i}_raw]")
            audio_filters.append(
                f"[a{prev_label}][a{i}_raw]acrossfade=d={duration:.2f}[a{i}]"
            )
            prev_label = i

    # Calculate offsets for xfade
    durations = []
    for i, path in enumerate(files):
        probe = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
        ], capture_output=True, text=True, timeout=30)
        try:
            d = float(probe.stdout.strip())
        except (ValueError, TypeError):
            d = 10.0
        durations.append(d)

    offset_strs = []
    total = 0.0
    for i in range(1, len(durations)):
        total += durations[i - 1] - duration
        offset_strs.append(f"offset{i}={total:.2f}")

    vf_parts = filter_parts + offset_strs
    vf = ";".join(vf_parts)
    af = ";".join(audio_filters)

    last = len(files) - 1
    subprocess.run([
        "ffmpeg", "-y"] + inputs + [
        "-filter_complex", f"{vf};{af}",
        "-map", f"[v{last}]", "-map", f"[a{last}]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "22",
        "-c:a", "aac", "-b:a", "192k", output,
    ], check=True, capture_output=True, timeout=600)
