import json, os, subprocess, logging, math, time
from typing import Optional, List, Tuple, Dict, Any
from app.models import Job, Clip, ClipCropOverrides
from app.config import DATA_DIR

logger = logging.getLogger("simbioclip.pipeline.render")

OUT_W = 1080
OUT_H = 1920

AR_DIMS = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:5": (864, 1080),
    "16:9": (1920, 1080),
}

CAPTION_STYLES = {"bold_pop", "neon", "minimal", "karaoke_highlight", "podcast"}

# --- Geometry utilities ---

def _even(n: float) -> int:
    return int(n) - (int(n) % 2)

def get_video_dimensions(path: str) -> Tuple[int, int]:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height", "-of", "json", path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    data = json.loads(r.stdout)
    s = data["streams"][0]
    return int(s["width"]), int(s["height"])

def _get_output_dims(aspect_ratio: str) -> Tuple[int, int]:
    return AR_DIMS.get(aspect_ratio, (OUT_W, OUT_H))

def compute_crop_box(
    frame_w: int, frame_h: int, target_ar: float,
    cx: float, cy: float, desired_h: float,
    overrides: Optional[ClipCropOverrides] = None,
) -> Tuple[int, int, int, int]:
    """Returns (x, y, w, h) integer crop box centered on (cx, cy)."""
    if overrides:
        cx += int(overrides.pan_x)
        cy += int(overrides.pan_y)
        if overrides.zoom > 0 and overrides.zoom != 1.0:
            desired_h = max(2, _even(desired_h / overrides.zoom))
    ch = min(desired_h, frame_h)
    cw = min(ch * target_ar, frame_w)
    ch = _even(min(ch, frame_h))
    cw = _even(min(cw, frame_w))
    x = max(0, _even(cx - cw / 2.0))
    y = max(0, _even(cy - ch / 2.0))
    if x + cw > frame_w:
        x = frame_w - cw
    if y + ch > frame_h:
        y = frame_h - ch
    return x, y, cw, ch

# --- Caption styles / ASS generation ---

_STYLES = {
    "bold_pop": {
        "font": "Arial Black,Impact,Helvetica",
        "size": 58,
        "color": "&H00FFFFFF",
        "bold": 1,
        "outline_col": "&H00000000",
        "outline": 3,
        "shadow": 1,
        "shadow_col": "&H80000000",
        "alignment": 2,
    },
    "neon": {
        "font": "Arial,Helvetica",
        "size": 54,
        "color": "&H0000FFFF",
        "bold": 0,
        "outline_col": "&H00FF00FF",
        "outline": 4,
        "shadow": 2,
        "shadow_col": "&H80FF00FF",
        "alignment": 2,
    },
    "minimal": {
        "font": "Arial,Helvetica",
        "size": 44,
        "color": "&H00FFFFFF",
        "bold": 0,
        "outline_col": "&H00000000",
        "outline": 1,
        "shadow": 0,
        "shadow_col": "&H80000000",
        "alignment": 2,
    },
    "karaoke_highlight": {
        "font": "Arial,Helvetica",
        "size": 58,
        "color": "&H00FFFFFF",
        "bold": 1,
        "outline_col": "&H00000000",
        "outline": 3,
        "shadow": 1,
        "shadow_col": "&H80000000",
        "alignment": 2,
        "karaoke": True,
    },
    "podcast": {
        "font": "Arial,Helvetica",
        "size": 62,
        "color": "&H00FFFFFF",
        "bold": 1,
        "outline_col": "&H00000000",
        "outline": 3,
        "shadow": 1,
        "shadow_col": "&H80000000",
        "alignment": 8,
    },
}

def _ms(sec: float) -> int:
    return max(0, int(round(sec * 1000)))

def _ass_escape(text: str) -> str:
    return (text
            .replace("{", "\\{")
            .replace("}", "\\}")
            .replace("\\n", "\\N"))

def generate_ass_file(
    segments: List[Dict],
    clip_start: float,
    clip_end: float,
    hook_text: str,
    output_path: str,
    speaker_segments: Optional[List[Dict]] = None,
    out_w: int = OUT_W,
    out_h: int = OUT_H,
    render_start: Optional[float] = None,
    caption_style: str = "bold_pop",
    silence_ranges_in_clip: Optional[List[List[float]]] = None,
    emphasis: Optional[List[Dict]] = None,
):
    style = _STYLES.get(caption_style, _STYLES["bold_pop"])
    name = "CaptionStyle"
    fmt = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
           "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
           "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
           "Alignment, MarginL, MarginR, MarginV, Encoding")
    sline = (f"Style: {name},{style['font']},{style['size']},{style['color']},"
             f"&H00FFFFFF,{style['outline_col']},{style['shadow_col']},"
             f"{style['bold']},0,0,0,100,100,0,0,1,{style['outline']},"
             f"{style['shadow']},{style['alignment']},10,10,50,1")
    margin_v = 60 if style['alignment'] == 2 else 180
    sline2 = (f"Style: Hook,{style['font']},48,&H00FFFFFF,"
              f"&H00FFFFFF,&H00000000,&H80000000,"
              f"{style['bold']},0,0,0,100,100,0,0,1,2,0,8,10,10,140,1")

    rs = render_start if render_start is not None else clip_start

    # --- Build section lines ---
    lines = []
    hook = (hook_text or "").strip()
    if hook:
        dur = min(clip_end - clip_start, 2.5)
        lines.append(f"Dialogue: 0,0:00:00.00,{_ms(dur)},Hook,,0,0,0,,{_ass_escape(hook.upper())}")

    emoji_list = emphasis or []
    for em in emoji_list:
        et = em.get("t", 0)
        emoji_char = em.get("emoji", "")
        if not emoji_char:
            continue
        eta = max(0.0, et - rs)
        start_ms = _ms(eta)
        end_ms = _ms(eta + 1.0)
        lines.append(f"Dialogue: 0,{start_ms},{end_ms},{name},,0,0,0,,{emoji_char}")

    target_segments = speaker_segments if speaker_segments is not None else segments
    karaoke = style.get("karaoke", False)

    for seg in target_segments:
        if "words" in seg and seg["words"] and karaoke:
            for w in seg["words"]:
                ws = max(0.0, w["start"] - rs)
                we = max(0.0, w["end"] - rs)
                if we <= ws:
                    continue
                txt = _ass_escape(w.get("text", ""))
                lines.append(f"Dialogue: 0,{_ms(ws)},{_ms(we)},{name},,0,0,0,,{txt}")
        else:
            s = max(0.0, seg["start"] - rs)
            e = max(0.0, seg["end"] - rs)
            if e <= s:
                continue
            txt = _ass_escape(seg.get("text", ""))
            lines.append(f"Dialogue: 0,{_ms(s)},{_ms(e)},{name},,0,0,0,,{txt}")

    content = "\n".join([
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "",
        "[V4+ Styles]",
        fmt,
        sline,
        sline2,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ] + lines)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(content)

# --- Layout filter builders ---

def build_split_cam_filter(face_box: Tuple[int, int, int, int], w: int, h: int,
                           facecam_panel_h: int, gameplay_panel_h: int,
                           facecam_scale: float, escaped_ass: str, out_w: int = OUT_W) -> str:
    fx, fy, fw, fh = face_box
    fcx, fcy = fx + fw / 2.0, fy + fh / 2.0
    top_ar = out_w / facecam_panel_h
    bot_ar = out_w / gameplay_panel_h
    cam_x, cam_y, cam_w, cam_h = compute_crop_box(w, h, top_ar, fcx, fcy, fh * facecam_scale)
    gp_x, gp_y, gp_w, gp_h = compute_crop_box(w, h, bot_ar, w / 2.0, h / 2.0, h)
    return (f"[0:v]split=2[t_raw][b_raw];"
            f"[t_raw]crop={cam_w}:{cam_h}:{cam_x}:{cam_y},scale={out_w}:{facecam_panel_h},setsar=1[top];"
            f"[b_raw]crop={gp_w}:{gp_h}:{gp_x}:{gp_y},scale={out_w}:{gameplay_panel_h},setsar=1[bottom];"
            f"[top][bottom]vstack=inputs=2,ass='{escaped_ass}'")

def build_center_crop_filter(w: int, h: int, crop_w: int, crop_h: int,
                             escaped_ass: str, out_w: int = OUT_W, out_h: int = OUT_H,
                             overrides: Optional[ClipCropOverrides] = None) -> str:
    cx, cy = max(0, _even((w - crop_w) / 2)), max(0, _even((h - crop_h) / 2))
    if overrides:
        cx += int(overrides.pan_x)
        cy += int(overrides.pan_y)
        if overrides.zoom > 0 and overrides.zoom != 1.0:
            new_w = max(2, _even(crop_w / overrides.zoom))
            new_h = max(2, _even(crop_h / overrides.zoom))
            cx = max(0, _even(cx + (crop_w - new_w) / 2))
            cy = max(0, _even(cy + (crop_h - new_h) / 2))
            crop_w, crop_h = new_w, new_h
        cx = max(0, min(cx, w - crop_w))
        cy = max(0, min(cy, h - crop_h))
    return f"crop={crop_w}:{crop_h}:{cx}:{cy},scale={out_w}:{out_h},setsar=1,ass='{escaped_ass}'"

def build_inset_filter(face_box: Tuple[int,int,int,int], w: int, h: int,
                       inset_w: int, inset_h: int, inset_position: str,
                       facecam_scale: float, escaped_ass: str,
                       out_w: int = OUT_W, out_h: int = OUT_H) -> str:
    fx, fy, fw, fh = face_box
    fcx, fcy = fx + fw / 2.0, fy + fh / 2.0
    mx, my, mw, mh = compute_crop_box(w, h, out_w / out_h, w / 2.0, h / 2.0, h)
    ix, iy, iw_h, ih = compute_crop_box(w, h, inset_w / inset_h, fcx, fcy, fh * facecam_scale)
    ox = out_w - inset_w - 24
    oy = 24
    return (f"[0:v]split=2[main_raw][inset_raw];"
            f"[main_raw]crop={mw}:{mh}:{mx}:{my},scale={out_w}:{out_h},setsar=1[main];"
            f"[inset_raw]crop={iw_h}:{ih}:{ix}:{iy},scale={inset_w}:{inset_h},setsar=1[inset];"
            f"[main][inset]overlay={ox}:{oy},ass='{escaped_ass}'")

def build_passthrough_filter(escaped_ass: str, out_w: int = OUT_W, out_h: int = OUT_H) -> str:
    return f"scale={out_w}:{out_h},setsar=1,ass='{escaped_ass}'"

def build_face_track_filter(face_box: Tuple[int,int,int,int], w: int, h: int,
                            facecam_scale: float, escaped_ass: str,
                            out_w: int = OUT_W, out_h: int = OUT_H,
                            overrides: Optional[ClipCropOverrides] = None) -> str:
    fx, fy, fw, fh = face_box
    fcx, fcy = fx + fw / 2.0, fy + fh / 2.0
    cx, cy, cw_val, ch_val = compute_crop_box(w, h, out_w / out_h, fcx, fcy, fh * facecam_scale, overrides)
    return f"crop={cw_val}:{ch_val}:{cx}:{cy},scale={out_w}:{out_h},setsar=1,ass='{escaped_ass}'"

def _build_polyline_expr(points: List[Tuple[float, float]]) -> str:
    if not points:
        return "0"
    if len(points) == 1:
        return f"{points[0][1]:.1f}"
    parts = []
    parts.append(f"if(lt(t\\,{points[0][0]:.3f})\\,{points[0][1]:.1f}")
    for i in range(len(points) - 1):
        t1, v1 = points[i]
        t2, v2 = points[i + 1]
        denom = max(0.001, t2 - t1)
        slope = (v2 - v1) / denom
        parts.append(f"\\,if(lt(t\\,{t2:.3f})\\,{v1:.1f}+({slope:.4f})*(t-{t1:.3f})")
    parts.append(f"\\,{points[-1][1]:.1f}")
    parts.append(")" * len(points))
    return "".join(parts)

def build_dynamic_face_track_filter(
    trajectory: List[Tuple[float, int, int, int, int]],
    w: int, h: int, facecam_scale: float,
    escaped_ass: str, render_start: float,
    out_w: int = OUT_W, out_h: int = OUT_H,
    overrides: Optional[ClipCropOverrides] = None,
) -> Optional[str]:
    if not trajectory or len(trajectory) < 2:
        return None
    target_ar = out_w / out_h
    crops = []
    for t_abs, fx, fy, fw, fh in trajectory:
        fcx, fcy = fx + fw / 2.0, fy + fh / 2.0
        desired_h = fh * facecam_scale
        cx, cy, cw, ch = compute_crop_box(w, h, target_ar, fcx, fcy, desired_h, overrides)
        crops.append((t_abs, cx, cy, cw, ch))
    cws = sorted(c[3] for c in crops)
    chs = sorted(c[4] for c in crops)
    median_cw = cws[len(cws) // 2]
    median_ch = chs[len(chs) // 2]
    if median_cw < 2 or median_ch < 2:
        return None
    median_cw -= median_cw % 2
    median_ch -= median_ch % 2
    knots_x: List[Tuple[float, float]] = []
    knots_y: List[Tuple[float, float]] = []
    offset_x = overrides.pan_x if overrides else 0.0
    offset_y = overrides.pan_y if overrides else 0.0
    zoom_factor = overrides.zoom if overrides and overrides.zoom > 0 else 1.0
    for t_abs, fx, fy, fw, fh in trajectory:
        fcx = fx + fw / 2.0 + offset_x
        fcy = fy + fh / 2.0 + offset_y
        x = max(0.0, min(w - median_cw, fcx - median_cw / 2.0))
        y = max(0.0, min(h - median_ch, fcy - median_ch / 2.0))
        t_rel = max(0.0, t_abs - render_start)
        knots_x.append((t_rel, x))
        knots_y.append((t_rel, y))
    x_expr = _build_polyline_expr(knots_x)
    y_expr = _build_polyline_expr(knots_y)
    return (
        f"crop={median_cw}:{median_ch}:'{x_expr}':'{y_expr}',"
        f"scale={out_w}:{out_h},setsar=1,ass='{escaped_ass}'"
    )

def build_podcast_dual_filter(face_boxes: List[Tuple[int,int,int,int]],
                              panel_w: int, w: int, h: int, facecam_scale: float,
                              escaped_ass: str, out_h: int = OUT_H) -> str:
    panel_h = out_h
    ar = panel_w / panel_h
    n = len(face_boxes)
    labels = [chr(ord("a") + i) for i in range(n)]
    split_out = "".join(f"[{l}_raw]" for l in labels)
    parts = [f"[0:v]split={n}{split_out}"]
    for i, (fx, fy, fw, fh) in enumerate(face_boxes):
        fcx, fcy = fx + fw / 2.0, fy + fh / 2.0
        cx, cy, cw_val, ch_val = compute_crop_box(w, h, ar, fcx, fcy, fh * facecam_scale)
        l = labels[i]
        parts.append(f"[{l}_raw]crop={cw_val}:{ch_val}:{cx}:{cy},scale={panel_w}:{panel_h},setsar=1[{l}]")
    stack_in = "".join(f"[{l}]" for l in labels)
    parts.append(f"{stack_in}hstack=inputs={n},ass='{escaped_ass}'")
    return ";".join(parts)

def build_podcast_stack_filter(face_boxes: List[Tuple[int,int,int,int]],
                               w: int, h: int, facecam_scale: float,
                               escaped_ass: str, out_w: int = OUT_W, out_h: int = OUT_H) -> str:
    n = len(face_boxes)
    base_h = out_h // n
    base_h -= base_h % 2
    panel_hs = [base_h] * n
    panel_hs[-1] = out_h - base_h * (n - 1)
    panel_hs[-1] -= panel_hs[-1] % 2
    labels = [chr(ord("a") + i) for i in range(n)]
    split_out = "".join(f"[{l}_raw]" for l in labels)
    parts = [f"[0:v]split={n}{split_out}"]
    for i, (fx, fy, fw, fh) in enumerate(face_boxes):
        ph = panel_hs[i]
        ar = out_w / ph
        fcx, fcy = fx + fw / 2.0, fy + fh / 2.0
        cx, cy, cw_val, ch_val = compute_crop_box(w, h, ar, fcx, fcy, fh * facecam_scale)
        l = labels[i]
        parts.append(f"[{l}_raw]crop={cw_val}:{ch_val}:{cx}:{cy},scale={out_w}:{ph},setsar=1[{l}]")
    stack_in = "".join(f"[{l}]" for l in labels)
    parts.append(f"{stack_in}vstack=inputs={n},ass='{escaped_ass}'")
    return ";".join(parts)

# --- Rendering ---

def _build_silence_skip_filter_complex(
    layout_vf: str,
    silence_rel: List[List[float]],
    audio_filters: List[str],
) -> Tuple[str, str, str]:
    range_expr = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in silence_rel)
    v_select = f"select='not({range_expr})',setpts=N/FRAME_RATE/TB"
    a_select = f"aselect='not({range_expr})',asetpts=N/SR/TB"
    if layout_vf.startswith("[0:v]"):
        layout_v = layout_vf.replace("[0:v]", f"[0:v]{v_select},", 1) + "[vout]"
    else:
        layout_v = f"[0:v]{v_select},{layout_vf}[vout]"
    a_chain = ",".join([a_select] + list(audio_filters))
    audio_v = f"[0:a]{a_chain}[aout]"
    return f"{layout_v};{audio_v}", "[vout]", "[aout]"


def _apply_thumbnail_overlay(
    video_path: str,
    thumbnail_path: str,
    out_w: int,
    out_h: int,
    duration: float,
) -> None:
    """Overlay thumbnail image at the beginning of the video (first 0.1s)."""
    tmp_path = video_path + ".thumb_tmp.mp4"
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", video_path,
        "-i", thumbnail_path,
        "-filter_complex",
        f"[1:v]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
        f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:color=black[thumb];"
        f"[0:v][thumb]overlay=0:0:enable='lte(t,0.1)'",
        "-c:v", "libx264", "-preset", "superfast", "-crf", "22",
        "-c:a", "copy",
        "-y", tmp_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        os.replace(tmp_path, video_path)
        logger.info(f"Thumbnail overlay applied to {video_path}")
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode(errors="ignore")[:500]
        logger.error(f"Thumbnail overlay failed for {video_path}: {stderr}")
        if os.path.exists(tmp_path):
            try: os.remove(tmp_path)
            except: pass
        raise


def render_clip_ffmpeg(
    video_path: str, clip: Clip, segments: List[Dict[str, Any]],
    job_dir: str, crop_w: int, crop_h: int, w: int, h: int,
    layout_params: Dict[str, Any] = None,
    progress_callback=None,
    aspect_ratio: str = "9:16",
    audio_ducking: bool = False,
    speaker_segments: Optional[List[Dict[str, Any]]] = None,
    trim_start: Optional[float] = None,
    trim_end: Optional[float] = None,
    caption_style: str = "bold_pop",
    silence_ranges: Optional[List[List[float]]] = None,
    dense_cut: bool = False,
    crop_overrides: Optional[ClipCropOverrides] = None,
    thumbnail_overlay_path: Optional[str] = None,
) -> str:
    out_w, out_h = _get_output_dims(aspect_ratio)
    clips_dir = os.path.join(job_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)
    output_path = os.path.join(clips_dir, f"{clip.id}.mp4")
    ass_path = os.path.join(clips_dir, f"{clip.id}.ass")

    render_start = clip.start
    render_duration = clip.duration
    if trim_start is not None:
        render_start = trim_start
        render_duration = (trim_end - trim_start) if trim_end is not None else (clip.end - trim_start)
    elif trim_end is not None:
        render_duration = trim_end - clip.start

    silence_abs: List[List[float]] = []
    silence_rel: List[List[float]] = []
    if dense_cut and silence_ranges:
        for sr in silence_ranges:
            try:
                s = max(float(sr[0]), render_start)
                e = min(float(sr[1]), render_start + render_duration)
            except (TypeError, ValueError, IndexError):
                continue
            if e - s > 0.2:
                silence_abs.append([round(s, 3), round(e, 3)])
                silence_rel.append([round(s - render_start, 3), round(e - render_start, 3)])

    generate_ass_file(
        segments, clip.start, clip.end, clip.hook, ass_path,
        speaker_segments=speaker_segments, out_w=out_w, out_h=out_h,
        render_start=render_start,
        caption_style=caption_style,
        silence_ranges_in_clip=silence_abs,
        emphasis=clip.emphasis or [],
    )

    escaped_ass = ass_path.replace("\\", "/").replace(":", "\\:")
    escaped_ass = escaped_ass.replace("'", "'\\\\''")

    lt = layout_params.get("type") if layout_params else None

    if lt == "passthrough":
        vf = build_passthrough_filter(escaped_ass, out_w, out_h)
    elif lt == "split_cam" and layout_params.get("face_box"):
        fb = layout_params["face_box"]
        vf = build_split_cam_filter(
            fb, w, h,
            layout_params.get("facecam_panel_h", 720),
            layout_params.get("gameplay_panel_h", out_h - 720),
            layout_params.get("facecam_scale", 3.2),
            escaped_ass, out_w)
    elif lt == "inset" and layout_params.get("face_box"):
        fb = layout_params["face_box"]
        vf = build_inset_filter(
            fb, w, h,
            layout_params.get("inset_w", 240),
            layout_params.get("inset_h", 426),
            layout_params.get("inset_position", "bottom-right"),
            layout_params.get("facecam_scale", 2.5),
            escaped_ass, out_w, out_h)
    elif lt == "podcast_stack" and layout_params.get("face_boxes"):
        fbs = layout_params["face_boxes"]
        vf = build_podcast_stack_filter(
            fbs, w, h,
            layout_params.get("facecam_scale", 2.2),
            escaped_ass, out_w, out_h)
    elif lt == "podcast_dual" and layout_params.get("face_boxes"):
        fbs = layout_params["face_boxes"]
        pw = layout_params.get("panel_w", out_w // 2)
        vf = build_podcast_dual_filter(
            fbs, pw, w, h,
            layout_params.get("facecam_scale", 2.5),
            escaped_ass, out_h)
    elif lt == "face_track" and layout_params.get("face_box"):
        fb = layout_params["face_box"]
        facecam_scale = layout_params.get("facecam_scale", 2.5)
        vf = None
        try:
            from app.pipeline.face_detect import detect_face_trajectory
            traj = detect_face_trajectory(video_path, render_start, render_start + render_duration)
            if traj:
                vf = build_dynamic_face_track_filter(
                    traj, w, h, facecam_scale,
                    escaped_ass, render_start, out_w, out_h,
                    overrides=crop_overrides,
                )
                if vf:
                    logger.info(f"Dynamic face track: {len(traj)} knots")
        except Exception as e:
            logger.warning(f"Dynamic face track failed, using static box: {e}")
            vf = None
        if vf is None:
            vf = build_face_track_filter(
                fb, w, h, facecam_scale,
                escaped_ass, out_w, out_h, overrides=crop_overrides)
    else:
        vf = build_center_crop_filter(w, h, crop_w, crop_h, escaped_ass, out_w, out_h, overrides=crop_overrides)

    clip_end_render = render_start + render_duration
    logger.info(
        f"Rendering {clip.id} [{render_start:.1f}s – {clip_end_render:.1f}s] "
        f"({render_duration:.1f}s) style={caption_style} dense_cut={dense_cut and bool(silence_rel)}"
    )

    audio_filters = ["loudnorm=I=-16:TP=-1.5:LRA=11"]
    if audio_ducking:
        logger.info("audio_ducking flag is currently a no-op (requires BG music track).")

    cmd = ["ffmpeg", "-y", "-progress", "pipe:1", "-nostats",
           "-ss", f"{render_start:.3f}", "-t", f"{render_duration:.3f}",
           "-i", video_path]

    if silence_rel:
        fc, vout, aout = _build_silence_skip_filter_complex(vf, silence_rel, audio_filters)
        cmd.extend(["-filter_complex", fc, "-map", vout, "-map", aout])
    else:
        cmd.extend(["-vf", vf, "-af", ",".join(audio_filters)])

    cmd.extend(["-c:v", "libx264", "-preset", "superfast", "-crf", "22",
                "-c:a", "aac", "-b:a", "192k"])
    cmd.append(output_path)

    stderr_path = os.path.join(job_dir, "clips", f"{clip.id}_ffmpeg_stderr.log")
    try:
        with open(stderr_path, "w") as stderr_file:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr_file, text=True)
            last_pct = 0
            for line in proc.stdout:
                if line.startswith("out_time_us="):
                    try:
                        us = int(line.strip().split("=")[1])
                        pct = min(int(us / 1_000_000 / render_duration * 100), 99)
                        if pct > last_pct:
                            last_pct = pct
                            if progress_callback:
                                progress_callback(pct)
                    except (ValueError, IndexError):
                        pass
            proc.wait()
        if proc.returncode != 0:
            with open(stderr_path, "r") as f:
                stderr_out = f.read()
            logger.error(f"FFmpeg exit {proc.returncode} for {clip.id}: {stderr_out}")
            raise RuntimeError(f"FFmpeg exited with code {proc.returncode} (see stderr above)")
    except RuntimeError:
        raise
    except Exception as e:
        logger.error(f"FFmpeg render failed for {clip.id}: {e}")
        raise RuntimeError(f"FFmpeg render failed: {e}")
    finally:
        try: os.remove(stderr_path)
        except: pass

    if os.path.exists(ass_path):
        try: os.remove(ass_path)
        except: pass

    # Apply thumbnail overlay after main render (2-pass approach)
    if thumbnail_overlay_path and os.path.exists(thumbnail_overlay_path):
        try:
            _apply_thumbnail_overlay(output_path, thumbnail_overlay_path, out_w, out_h, render_duration)
        except Exception as e:
            logger.warning(f"Thumbnail overlay skipped for {clip.id}: {e}")

    # Auto-generate thumbnail (best-effort; never fails the render)
    try:
        from app.pipeline.thumbnail import generate_thumbnail
        thumbs_dir = os.path.join(job_dir, "thumbnails")
        os.makedirs(thumbs_dir, exist_ok=True)
        thumb_path = os.path.join(thumbs_dir, f"{clip.id}.jpg")
        generate_thumbnail(
            clip_path=output_path,
            output_path=thumb_path,
            hook_text=clip.hook,
            clip_duration=render_duration,
            width=out_w, height=out_h,
        )
    except Exception as e:
        logger.debug(f"Thumbnail generation skipped for {clip.id}: {e}")

    logger.info(f"Rendered: {output_path}")
    return output_path


# --- Active speaker tracking ---

def _group_speaker_segments(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not segments:
        return []
    groups = []
    cur_sp = segments[0].get("speaker", "UNKNOWN")
    cur = [segments[0]]
    for seg in segments[1:]:
        sp = seg.get("speaker", "UNKNOWN")
        if sp == cur_sp:
            cur.append(seg)
        else:
            groups.append({"speaker": cur_sp, "segments": cur})
            cur_sp = sp
            cur = [seg]
    if cur:
        groups.append({"speaker": cur_sp, "segments": cur})
    return groups


def render_active_speaker_clip(
    video_path: str, clip: Clip, segments: List[Dict[str, Any]],
    job_dir: str, crop_w: int, crop_h: int, w: int, h: int,
    speaker_segments: List[Dict[str, Any]],
    all_face_boxes: List[Tuple[int, int, int, int]],
    aspect_ratio: str, audio_ducking: bool,
    progress_callback,
    caption_style: str = "bold_pop",
    crop_overrides: Optional[ClipCropOverrides] = None,
    thumbnail_overlay_path: Optional[str] = None,
) -> str:
    from app.pipeline.face_detect import detect_face_camera
    from app.pipeline.layout_engine import get_layout_params

    groups = _group_speaker_segments(speaker_segments)
    clips_dir = os.path.join(job_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)
    sub_paths = []
    speaker_face_cache = {}
    total_sub = len(groups)
    out_w, out_h = _get_output_dims(aspect_ratio)

    for idx, group in enumerate(groups):
        sp = group["speaker"]
        g_start = group["segments"][0]["start"]
        g_end = group["segments"][-1]["end"]

        if sp not in speaker_face_cache:
            try:
                fb = detect_face_camera(video_path, g_start, g_end)
                if fb:
                    _, _, fw, fh = fb
                    if fw <= w * 0.5 and fh <= h * 0.5:
                        speaker_face_cache[sp] = fb
            except Exception:
                pass
        sub_face = speaker_face_cache.get(sp)

        sub_layout = get_layout_params(
            content_type="podcast",
            layout_mode="talking_head" if sub_face else "center_crop",
            face_box=sub_face,
            frame_w=w, frame_h=h,
            face_boxes=all_face_boxes if len(all_face_boxes) > 1 else None,
            speaker_count=len(groups),
        )

        sub_id = f"{clip.id}_spk{idx}"
        sub_path = os.path.join(clips_dir, f"{sub_id}.mp4")
        sub_duration = g_end - g_start

        ass_path = os.path.join(clips_dir, f"{sub_id}.ass")
        generate_ass_file(
            segments, clip.start, clip.end, clip.hook, ass_path,
            speaker_segments=group["segments"],
            out_w=out_w, out_h=out_h, render_start=g_start,
            caption_style=caption_style,
            emphasis=clip.emphasis or [],
        )

        escaped_ass = ass_path.replace("\\", "/").replace(":", "\\:")
        escaped_ass = escaped_ass.replace("'", "'\\\\''")

        if sub_face:
            vf = build_face_track_filter(sub_face, w, h, 2.5, escaped_ass, out_w, out_h)
        else:
            vf = build_center_crop_filter(w, h, crop_w, crop_h, escaped_ass, out_w, out_h)

        cmd = ["ffmpeg", "-y", "-progress", "pipe:1", "-nostats",
               "-ss", f"{g_start:.3f}", "-t", f"{sub_duration:.3f}",
               "-i", video_path]
        cmd.extend(["-vf", vf, "-af", "loudnorm=I=-16:TP=-1.5:LRA=11"])
        cmd.extend(["-c:v", "libx264", "-preset", "superfast", "-crf", "22",
                    "-c:a", "aac", "-b:a", "192k"])
        if audio_ducking:
            logger.debug("audio_ducking no-op in active speaker render (no BG music yet)")
        cmd.append(sub_path)

        subprocess.run(cmd, check=True, capture_output=True, timeout=600)
        if os.path.exists(ass_path):
            try: os.remove(ass_path)
            except: pass
        sub_paths.append(sub_path)
        if progress_callback:
            progress_callback(int((idx + 1) / total_sub * 100))

    # Concat all sub-clips
    output_path = os.path.join(clips_dir, f"{clip.id}.mp4")
    concat_file = os.path.join(clips_dir, f"{clip.id}_concat.txt")
    with open(concat_file, "w") as f:
        for sp in sub_paths:
            f.write(f"file '{os.path.basename(sp)}'\n")

    try:
        subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", concat_file, "-c", "copy", output_path
        ], check=True, capture_output=True, timeout=300)
    except Exception as e:
        logger.error(f"Concat failed for {clip.id}: {e}")
        if sub_paths:
            import shutil
            shutil.copy(sub_paths[0], output_path)
        else:
            raise

    for sp in sub_paths:
        try: os.remove(sp)
        except: pass
    try: os.remove(concat_file)
    except: pass

    # Apply thumbnail overlay after concat (2-pass approach)
    if thumbnail_overlay_path and os.path.exists(thumbnail_overlay_path):
        try:
            _apply_thumbnail_overlay(output_path, thumbnail_overlay_path, out_w, out_h, clip.duration)
        except Exception as e:
            logger.warning(f"Thumbnail overlay skipped for {clip.id}: {e}")

    # Auto-generate thumbnail (best-effort)
    try:
        from app.pipeline.thumbnail import generate_thumbnail
        thumbs_dir = os.path.join(job_dir, "thumbnails")
        os.makedirs(thumbs_dir, exist_ok=True)
        thumb_path = os.path.join(thumbs_dir, f"{clip.id}.jpg")
        out_w_t, out_h_t = _get_output_dims(aspect_ratio)
        generate_thumbnail(
            clip_path=output_path, output_path=thumb_path,
            hook_text=clip.hook, clip_duration=clip.duration,
            width=out_w_t, height=out_h_t,
        )
    except Exception as e:
        logger.debug(f"Thumbnail generation skipped for {clip.id}: {e}")

    logger.info(f"Active-speaker clip rendered: {output_path} ({len(groups)} segments)")
    return output_path


def render_one_clip(
    job: Job, clip: Clip, video_path: str,
    segments: List[Dict[str, Any]],
    diarized: Optional[List[Dict[str, Any]]] = None,
    override_layout: Optional[str] = None,
    override_caption_style: Optional[str] = None,
    progress_callback=None,
) -> str:
    from app.pipeline.face_detect import detect_face_camera, detect_all_faces
    from app.pipeline.layout_engine import get_layout_params
    from app.pipeline.diarization import get_speaker_segments

    job_dir = job.get_dir()
    w, h = get_video_dimensions(video_path)
    aspect_ratio = job.aspect_ratio or "9:16"
    ar_w, ar_h = _get_output_dims(aspect_ratio)
    target_ar = ar_w / ar_h
    if w / h > target_ar:
        crop_h = h
        crop_w = int(h * target_ar)
    else:
        crop_w = w
        crop_h = int(w / target_ar)
    crop_w -= crop_w % 2
    crop_h -= crop_h % 2

    detect_faces = w > h
    face_box: Optional[Tuple[int, int, int, int]] = None
    face_boxes: List[Tuple[int, int, int, int]] = []

    if detect_faces:
        try: face_boxes = detect_all_faces(video_path, clip.start, clip.end)
        except Exception as e: logger.error(f"Multi-face detect failed: {e}")
        if not face_boxes:
            try: face_boxes = detect_all_faces(video_path)
            except Exception: pass
        try: face_box = detect_face_camera(video_path, clip.start, clip.end)
        except Exception: pass
        if not face_box:
            try: face_box = detect_face_camera(video_path)
            except Exception: pass
        if face_box:
            _, _, fw, fh = face_box
            if fw > w * 0.5 or fh > h * 0.5:
                face_box = None
            elif face_box not in face_boxes:
                face_boxes.insert(0, face_box)

    sp_segments = None
    multi_speaker = False
    if diarized:
        sp_segments = get_speaker_segments(diarized, clip.start, clip.end)
        if sp_segments:
            speakers = set(s["speaker"] for s in sp_segments)
            clip.speaker_label = ", ".join(sorted(speakers))
            clip.speaker_segments = sp_segments
            multi_speaker = len(speakers) > 1

    caption_style = override_caption_style or clip.caption_style_override or job.caption_style or "bold_pop"
    layout_mode = override_layout or clip.layout_mode_override or job.layout_mode

    crop_overrides = clip.crop_overrides

    if multi_speaker and sp_segments and detect_faces and not override_layout:
        clip_path = render_active_speaker_clip(
            video_path=video_path, clip=clip, segments=segments,
            job_dir=job_dir, crop_w=crop_w, crop_h=crop_h,
            w=w, h=h, speaker_segments=sp_segments,
            all_face_boxes=face_boxes,
            aspect_ratio=aspect_ratio,
            audio_ducking=job.audio_ducking or False,
            progress_callback=progress_callback,
            caption_style=caption_style,
            crop_overrides=crop_overrides,
            thumbnail_overlay_path=clip.thumbnail_image_path,
        )
        clip.layout_used = "Active Speaker"
        clip.facecam_detected = True
    else:
        layout_params = get_layout_params(
            content_type=job.content_type or "unknown",
            layout_mode=layout_mode,
            face_box=face_box,
            frame_w=w, frame_h=h,
            face_boxes=face_boxes if len(face_boxes) > 1 else None,
            speaker_count=job.speaker_count,
        )
        clip.facecam_detected = face_box is not None
        clip.layout_used = layout_params.get("label", "Center Crop")

        clip_path = render_clip_ffmpeg(
            video_path=video_path, clip=clip, segments=segments,
            job_dir=job_dir, crop_w=crop_w, crop_h=crop_h,
            w=w, h=h, layout_params=layout_params,
            progress_callback=progress_callback,
            aspect_ratio=aspect_ratio,
            audio_ducking=job.audio_ducking or False,
            speaker_segments=sp_segments,
            caption_style=caption_style,
            silence_ranges=job.silence_ranges or [],
            dense_cut=bool(job.dense_cut),
            trim_start=clip.trim_start, trim_end=clip.trim_end,
            crop_overrides=crop_overrides,
            thumbnail_overlay_path=clip.thumbnail_image_path,
        )

    clip.file_path = clip_path
    clip.download_url = f"/jobs/{job.id}/clips/{clip.id}"
    thumb_file = os.path.join(job_dir, "thumbnails", f"{clip.id}.jpg")
    if os.path.exists(thumb_file):
        clip.thumbnail_url = f"/jobs/{job.id}/clips/{clip.id}/thumb"
    return clip_path


def render_job_clips(
    job: Job, video_path: str, segments: List[Dict[str, Any]],
    diarized: Optional[List[Dict[str, Any]]] = None,
) -> None:
    job_dir = job.get_dir()
    w, h = get_video_dimensions(video_path)
    logger.info(f"Source video dimensions: {w}x{h}")

    detect_faces = w > h
    if detect_faces:
        from app.pipeline.face_detect import detect_face_camera, detect_all_faces
    from app.pipeline.layout_engine import get_layout_params
    from app.pipeline.diarization import get_speaker_segments

    def _valid_overlay(box):
        if not box: return None
        _, _, bw, bh = box
        if bw > w * 0.5 or bh > h * 0.5: return None
        return box

    _global_cache = {}
    def _global_face_box():
        if "box" not in _global_cache:
            box = None
            try: box = _valid_overlay(detect_face_camera(video_path))
            except Exception as e: logger.error(f"Global face detect failed: {e}")
            _global_cache["box"] = box
        return _global_cache["box"]

    _global_faces_cache = []
    def _global_all_faces():
        if not _global_faces_cache:
            try: _global_faces_cache.extend(detect_all_faces(video_path))
            except Exception as e: logger.error(f"Global multi-face detect failed: {e}")
        return _global_faces_cache

    aspect_ratio = job.aspect_ratio or "9:16"
    ar_w, ar_h = _get_output_dims(aspect_ratio)
    target_ar = ar_w / ar_h

    if w / h > target_ar:
        crop_h = h
        crop_w = int(h * target_ar)
    else:
        crop_w = w
        crop_h = int(w / target_ar)
    crop_w -= crop_w % 2
    crop_h -= crop_h % 2

    for i, clip in enumerate(job.clips):
        job.status = f"rendering ({i+1}/{len(job.clips)})"
        job.save()

        face_box = None
        face_boxes: List[Tuple[int, int, int, int]] = []

        if detect_faces:
            try: face_boxes = detect_all_faces(video_path, clip.start, clip.end)
            except Exception as e: logger.error(f"Multi-face detect failed for {clip.id}: {e}")
            if not face_boxes:
                face_boxes = _global_all_faces()
            try: face_box = _valid_overlay(detect_face_camera(video_path, clip.start, clip.end))
            except Exception as e: logger.error(f"Face detect failed for {clip.id}: {e}")
            if not face_box:
                face_box = _global_face_box()
            if face_box and face_box not in face_boxes:
                face_boxes.insert(0, face_box)

        sp_segments = None
        multi_speaker = False
        if diarized:
            sp_segments = get_speaker_segments(diarized, clip.start, clip.end)
            if sp_segments:
                speakers = set(s["speaker"] for s in sp_segments)
                clip.speaker_label = ", ".join(sorted(speakers))
                clip.speaker_segments = sp_segments
                multi_speaker = len(speakers) > 1

        def _progress(pct):
            job.status = f"rendering ({i+1}/{len(job.clips)}) {pct}%"
            job.save()

        try:
            if multi_speaker and sp_segments and detect_faces:
                clip_path = render_active_speaker_clip(
                    video_path=video_path, clip=clip, segments=segments,
                    job_dir=job_dir, crop_w=crop_w, crop_h=crop_h,
                    w=w, h=h, speaker_segments=sp_segments,
                    all_face_boxes=face_boxes,
                    aspect_ratio=aspect_ratio, audio_ducking=job.audio_ducking or False,
                    progress_callback=_progress,
                    caption_style=job.caption_style or "bold_pop",
                    thumbnail_overlay_path=clip.thumbnail_image_path,
                )
                clip.layout_used = "Active Speaker"
                clip.facecam_detected = True
            else:
                layout_params = get_layout_params(
                    content_type=job.content_type or "unknown",
                    layout_mode=job.layout_mode,
                    face_box=face_box,
                    frame_w=w, frame_h=h,
                    face_boxes=face_boxes if len(face_boxes) > 1 else None,
                    speaker_count=job.speaker_count,
                )
                clip.facecam_detected = face_box is not None
                clip.layout_used = layout_params.get("label", "Center Crop")

                clip_path = render_clip_ffmpeg(
                    video_path=video_path, clip=clip, segments=segments,
                    job_dir=job_dir, crop_w=crop_w, crop_h=crop_h,
                    w=w, h=h, layout_params=layout_params,
                    progress_callback=_progress,
                    aspect_ratio=aspect_ratio,
                    audio_ducking=job.audio_ducking or False,
                    speaker_segments=sp_segments,
                    caption_style=job.caption_style or "bold_pop",
                    silence_ranges=job.silence_ranges or [],
                    dense_cut=bool(job.dense_cut),
                    trim_start=clip.trim_start, trim_end=clip.trim_end,
                    thumbnail_overlay_path=clip.thumbnail_image_path,
                )

            clip.file_path = clip_path
            clip.download_url = f"/jobs/{job.id}/clips/{clip.id}"
            thumb_file = os.path.join(job_dir, "thumbnails", f"{clip.id}.jpg")
            if os.path.exists(thumb_file):
                clip.thumbnail_url = f"/jobs/{job.id}/clips/{clip.id}/thumb"
        except Exception as e:
            logger.error(f"Failed to render clip {clip.id}: {e}")
            continue

    job.save()
