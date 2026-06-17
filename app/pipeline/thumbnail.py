import os
import logging
import subprocess
from typing import Optional

logger = logging.getLogger("simbioclip.pipeline.thumbnail")

# Try candidate fonts in order. First existing one wins. Used by ffmpeg drawtext.
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial.ttf",
]


def _pick_font() -> Optional[str]:
    for f in _FONT_CANDIDATES:
        if os.path.exists(f):
            return f
    return None


def _escape_drawtext(text: str) -> str:
    """Escape characters that have special meaning inside ffmpeg drawtext text="..." values."""
    # Order matters: escape backslashes first, then single quotes, colons, percent signs.
    return (
        text.replace("\\", "\\\\")
            .replace("'", r"\'")
            .replace(":", r"\:")
            .replace("%", r"\%")
    )


def generate_thumbnail(
    clip_path: str,
    output_path: str,
    hook_text: str = "",
    clip_duration: Optional[float] = None,
    width: int = 1080,
    height: int = 1920,
) -> Optional[str]:
    """
    Extracts a single frame near the start of the clip (after the hook overlay
    has faded so the actual content is visible) and optionally burns the hook
    text again in a thumbnail-friendly position.

    Returns the output path on success, or None on failure (non-fatal).
    """
    if not os.path.exists(clip_path):
        logger.warning(f"Cannot generate thumbnail; clip not found: {clip_path}")
        return None

    # Pick a frame ~2.2s in (just past the hook fade) so the thumbnail shows actual content.
    # Falls back to 30% of duration if the clip is short.
    if clip_duration and clip_duration < 4.0:
        seek_t = max(0.0, clip_duration * 0.4)
    else:
        seek_t = 2.2

    font = _pick_font()
    vf_parts = [f"scale={width}:{height}:force_original_aspect_ratio=increase",
                f"crop={width}:{height}"]

    hook = (hook_text or "").strip()
    if hook and font:
        # Render hook text near the bottom — large, white, with bold black box behind it.
        # Mirrors the in-video hook style so the thumbnail feels coherent with the clip.
        text_clean = _escape_drawtext(hook.upper())
        font_size = 72 if len(hook) > 18 else 88
        drawtext = (
            f"drawtext=fontfile='{font}':text='{text_clean}':"
            f"fontcolor=white:fontsize={font_size}:"
            f"x=(w-text_w)/2:y=h*0.72:"
            f"box=1:boxcolor=black@0.55:boxborderw=24:"
            f"borderw=4:bordercolor=black"
        )
        vf_parts.append(drawtext)

    vf = ",".join(vf_parts)

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{seek_t:.3f}",
        "-i", clip_path,
        "-frames:v", "1",
        "-vf", vf,
        "-q:v", "3",
        output_path,
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=30)
        return output_path
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode(errors="ignore")[:300]
        logger.warning(f"Thumbnail generation failed for {clip_path}: {stderr}")
        # Retry without drawtext in case the font filter broke
        if hook and font:
            try:
                fallback_vf = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error",
                     "-ss", f"{seek_t:.3f}", "-i", clip_path,
                     "-frames:v", "1", "-vf", fallback_vf, "-q:v", "3", output_path],
                    check=True, capture_output=True, timeout=30,
                )
                return output_path
            except Exception as e2:
                logger.warning(f"Thumbnail fallback also failed: {e2}")
        return None
    except Exception as e:
        logger.warning(f"Thumbnail generation error: {e}")
        return None
