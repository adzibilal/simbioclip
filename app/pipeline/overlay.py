import os
import logging
import subprocess
from typing import Optional

from app.pipeline.thumbnail import _pick_font, _escape_drawtext

logger = logging.getLogger("simbioclip.pipeline.overlay")


def _get_video_dims(video_path: str) -> tuple[int, int]:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
        video_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    parts = result.stdout.strip().split(",")
    if len(parts) == 2:
        return int(parts[0]), int(parts[1])
    return 1080, 1920


def render_image_watermark(
    input_path: str,
    output_path: str,
    watermark_path: str,
    pos_x: float = 0.85,
    pos_y: float = 0.05,
    opacity: float = 0.8,
    scale: float = 0.12,
) -> None:
    if not os.path.exists(watermark_path):
        raise FileNotFoundError(f"Watermark image not found: {watermark_path}")

    w, h = _get_video_dims(input_path)
    wm_w = int(w * scale)
    x = int(w * pos_x)
    y = int(h * pos_y)
    op = max(0.0, min(1.0, opacity))

    filter_complex = (
        f"[1:v]scale={wm_w}:-1:flags=lanczos,format=rgba,"
        f"colorchannelmixer=aa={op}[wm];"
        f"[0:v][wm]overlay={x}:{y}"
    )

    tmp = output_path + ".wm_tmp.mp4"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", input_path,
        "-i", watermark_path,
        "-filter_complex", filter_complex,
        "-c:v", "libx264", "-preset", "superfast", "-crf", "22",
        "-c:a", "copy",
        "-movflags", "+faststart",
        tmp,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        os.replace(tmp, output_path)
        logger.info(f"Image watermark applied to {output_path}")
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode(errors="ignore")[:500]
        logger.error(f"Image watermark failed: {stderr}")
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except: pass
        raise


def render_credit_watermark(
    input_path: str,
    output_path: str,
    channel_name: str,
    pos_x: float = 0.5,
    pos_y: float = 0.95,
    size: float = 0.022,
    opacity: float = 0.3,
) -> None:
    w, h = _get_video_dims(input_path)
    font_size = max(12, int(h * size))
    x = int(w * pos_x)
    y = int(h * pos_y)
    op = max(0.0, min(1.0, opacity))
    text = f"Source: {channel_name}"
    escaped = _escape_drawtext(text)
    font = _pick_font()
    if not font:
        logger.warning("No font found for credit watermark, skipping")
        return

    vf = (
        f"drawtext=fontfile='{font}':"
        f"text='{escaped}':"
        f"fontsize={font_size}:"
        f"fontcolor=white@{op}:"
        f"borderw=2:"
        f"bordercolor=black@{op}:"
        f"x={x}-(text_w/2):"
        f"y={y}-(text_h/2)"
    )

    tmp = output_path + ".credit_tmp.mp4"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", input_path,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "superfast", "-crf", "22",
        "-c:a", "copy",
        "-movflags", "+faststart",
        tmp,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        os.replace(tmp, output_path)
        logger.info(f"Credit watermark applied to {output_path}")
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode(errors="ignore")[:500]
        logger.error(f"Credit watermark failed: {stderr}")
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except: pass
        raise


def render_hook_overlay(
    input_path: str,
    output_path: str,
    hook_text: str,
    font_size: float = 0.045,
    font_color: str = "#00a000",
    bg_color: str = "#FFFFFF",
    corner_radius: int = 8,
    pos_x: float = 0.5,
    pos_y: float = 0.7,
    duration: float = 2.0,
) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        logger.error("Pillow required for hook overlay")
        return

    w, h = _get_video_dims(input_path)
    fs = max(16, int(w * font_size))
    x = int(w * pos_x)
    y = int(h * pos_y)

    font = None
    for candidate in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]:
        if os.path.exists(candidate):
            try:
                font = ImageFont.truetype(candidate, fs)
                break
            except Exception:
                continue
    if not font:
        font = ImageFont.load_default()

    max_w = int(w * 0.80)

    def wrap_line(text: str) -> list[str]:
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= max_w or " " not in text:
            return [text]
        words = text.split(" ")
        lines: list[str] = []
        current = ""
        for word in words:
            test = (current + " " + word).strip()
            tw = draw.textbbox((0, 0), test, font=font)[2]
            if tw > max_w and current:
                lines.append(current)
                current = word
            else:
                current = test
        if current:
            lines.append(current)
        return lines

    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    try:
        hex_to_rgb = lambda hex_str: tuple(int(hex_str.lstrip("#")[i:i+2], 16) for i in (0, 2, 4))
        bg_rgb = hex_to_rgb(bg_color)
        fg_rgb = hex_to_rgb(font_color)
    except Exception:
        bg_rgb = (255, 255, 255)
        fg_rgb = (0, 160, 0)

    raw_lines = hook_text.upper().split("\n")
    wrapped: list[str] = []
    for rl in raw_lines:
        wrapped.extend(wrap_line(rl))

    line_h = fs + int(fs * 0.2)
    total_h = len(wrapped) * line_h + 20
    pad = 12

    for i, line in enumerate(wrapped):
        bbox = draw.textbbox((0, 0), line, font=font)
        lw = bbox[2] - bbox[0]
        lh = bbox[3] - bbox[1]
        bx = x - lw // 2
        by = y + i * line_h - total_h // 2 + 10

        bx1 = bx - pad
        by1 = by - pad
        bx2 = bx + lw + pad
        by2 = by + lh + pad

        if corner_radius > 0:
            draw.rounded_rectangle([bx1, by1, bx2, by2], radius=corner_radius, fill=bg_rgb)
        else:
            draw.rectangle([bx1, by1, bx2, by2], fill=bg_rgb)

        draw.text((bx, by), line, font=font, fill=fg_rgb)

    overlay_png = output_path + ".hook_overlay.png"
    overlay.save(overlay_png)

    tmp = output_path + ".hook_tmp.mp4"
    filter_complex = f"[0:v][1:v]overlay=0:0:enable='lt(t,{duration})'[v]"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", input_path,
        "-i", overlay_png,
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-map", "0:a",
        "-c:v", "libx264", "-preset", "superfast", "-crf", "22",
        "-c:a", "copy",
        "-movflags", "+faststart",
        tmp,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        os.replace(tmp, output_path)
        logger.info(f"Hook overlay applied to {output_path}")
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode(errors="ignore")[:500]
        logger.error(f"Hook overlay failed: {stderr}")
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except: pass
        raise
    finally:
        if os.path.exists(overlay_png):
            try: os.remove(overlay_png)
            except: pass
