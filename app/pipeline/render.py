import os
import json
import logging
import subprocess
from typing import List, Dict, Any, Tuple, Optional
from app.models import Job, Clip, ClipCropOverrides

logger = logging.getLogger("simbioclip.pipeline.render")

OUT_W = 1080
OUT_H = 1920

AR_DIMS = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:5": (864, 1080),
    "16:9": (1920, 1080),
}

SPEAKER_COLORS = [
    "&H00FFB347",
    "&H0066B2FF",
    "&H0044FF44",
    "&H00FF6EB4",
    "&H00FFFF44",
    "&H00B266FF",
    "&H00FF4444",
    "&H0044FFFF",
]


# Caption style presets. Subtitle "primary" is the color of normal text; in
# karaoke_highlight, "secondary" is the BASE color and "primary" is the color
# that fills in word-by-word as \k tags advance.
CAPTION_STYLES: Dict[str, Dict[str, Any]] = {
    "bold_pop": {
        "subtitle": {"font": "Arial", "size": 60, "primary": "&H00FFFFFF", "secondary": "&H000000FF",
                     "outline_c": "&H00000000", "back": "&H00000000", "bold": 1,
                     "outline": 5, "shadow": 0, "alignment": 2, "margin_v": 350},
        "hook":     {"font": "Arial", "size": 68, "primary": "&H0000FFFF", "secondary": "&H000000FF",
                     "outline_c": "&H00000000", "back": "&H00000000", "bold": 1,
                     "outline": 5, "shadow": 0, "alignment": 2, "margin_v": 720,
                     "margin_l": 40, "margin_r": 40},
        "uppercase": True, "highlight_mode": False,
    },
    "neon": {
        "subtitle": {"font": "Arial", "size": 60, "primary": "&H00FFFF00", "secondary": "&H000000FF",
                     "outline_c": "&H00FF00FF", "back": "&H00000000", "bold": 1,
                     "outline": 4, "shadow": 2, "alignment": 2, "margin_v": 350},
        "hook":     {"font": "Arial", "size": 68, "primary": "&H00FF66FF", "secondary": "&H000000FF",
                     "outline_c": "&H0000FFFF", "back": "&H00000000", "bold": 1,
                     "outline": 5, "shadow": 2, "alignment": 2, "margin_v": 720,
                     "margin_l": 40, "margin_r": 40},
        "uppercase": True, "highlight_mode": False,
    },
    "minimal": {
        "subtitle": {"font": "Arial", "size": 54, "primary": "&H00FFFFFF", "secondary": "&H000000FF",
                     "outline_c": "&H80000000", "back": "&H00000000", "bold": 0,
                     "outline": 2, "shadow": 0, "alignment": 2, "margin_v": 320},
        "hook":     {"font": "Arial", "size": 60, "primary": "&H00FFFFFF", "secondary": "&H000000FF",
                     "outline_c": "&H80000000", "back": "&H00000000", "bold": 1,
                     "outline": 3, "shadow": 0, "alignment": 2, "margin_v": 720,
                     "margin_l": 40, "margin_r": 40},
        "uppercase": False, "highlight_mode": False,
    },
    "karaoke_highlight": {
        # Text starts as `secondary` (white) and turns `primary` (yellow) as \k passes
        "subtitle": {"font": "Arial", "size": 58, "primary": "&H0000FFFF", "secondary": "&H00FFFFFF",
                     "outline_c": "&H00000000", "back": "&H00000000", "bold": 1,
                     "outline": 5, "shadow": 0, "alignment": 2, "margin_v": 350},
        "hook":     {"font": "Arial", "size": 68, "primary": "&H0000FFFF", "secondary": "&H000000FF",
                     "outline_c": "&H00000000", "back": "&H00000000", "bold": 1,
                     "outline": 5, "shadow": 0, "alignment": 2, "margin_v": 720,
                     "margin_l": 40, "margin_r": 40},
        "uppercase": True, "highlight_mode": True,
        "phrase_max_chunks": 4, "phrase_max_chars": 30,
    },
    "podcast": {
        "subtitle": {"font": "Arial", "size": 56, "primary": "&H00FFFFFF", "secondary": "&H000000FF",
                     "outline_c": "&H00000000", "back": "&H00000000", "bold": 0,
                     "outline": 4, "shadow": 0, "alignment": 2, "margin_v": 350},
        "hook":     {"font": "Arial", "size": 64, "primary": "&H00FFFFFF", "secondary": "&H000000FF",
                     "outline_c": "&H00000000", "back": "&H00000000", "bold": 1,
                     "outline": 4, "shadow": 0, "alignment": 2, "margin_v": 720,
                     "margin_l": 40, "margin_r": 40},
        "uppercase": False, "highlight_mode": False,
    },
}


def _resolve_caption_style(name: Optional[str]) -> Dict[str, Any]:
    return CAPTION_STYLES.get((name or "bold_pop"), CAPTION_STYLES["bold_pop"])


def _ass_style_line(name: str, cfg: Dict[str, Any], primary_override: Optional[str] = None) -> str:
    """Build one V4+ Style line from a CAPTION_STYLES sub-dict."""
    primary = primary_override or cfg["primary"]
    margin_l = cfg.get("margin_l", 10)
    margin_r = cfg.get("margin_r", 10)
    return (
        f"Style: {name},{cfg['font']},{cfg['size']},"
        f"{primary},{cfg['secondary']},{cfg['outline_c']},{cfg.get('back', '&H00000000')},"
        f"{cfg['bold']},0,0,0,100,100,0,0,1,"
        f"{cfg['outline']},{cfg['shadow']},"
        f"{cfg['alignment']},{margin_l},{margin_r},{cfg['margin_v']},1"
    )


def _shift_for_silence(t_abs: float, silence_ranges: List[List[float]]) -> float:
    """Cumulative duration of silence_ranges that occur strictly before t_abs."""
    shift = 0.0
    for s, e in silence_ranges:
        if e <= t_abs:
            shift += (e - s)
        elif s < t_abs:
            shift += (t_abs - s)
            break
        else:
            break
    return shift


def _group_chunks_into_phrases(
    chunks: List[Dict[str, Any]],
    max_chunks: int = 4,
    max_chars: int = 30,
    max_gap: float = 0.6,
) -> List[List[Dict[str, Any]]]:
    """Group consecutive word chunks into karaoke phrases (one Dialogue per phrase)."""
    phrases: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    cur_chars = 0
    for c in chunks:
        c_text = c.get("text", "")
        c_chars = len(c_text)
        gap = (c["start"] - cur[-1]["end"]) if cur else 0.0
        prospective = cur_chars + (1 if cur else 0) + c_chars
        if cur and (len(cur) >= max_chunks or prospective > max_chars or gap > max_gap):
            phrases.append(cur)
            cur = [c]
            cur_chars = c_chars
        else:
            cur.append(c)
            cur_chars = prospective
    if cur:
        phrases.append(cur)
    return phrases


def _get_output_dims(aspect_ratio: str) -> Tuple[int, int]:
    return AR_DIMS.get(aspect_ratio, (OUT_W, OUT_H))


def _even(value) -> int:
    n = int(round(value))
    return n - (n % 2)


def compute_crop_box(
    frame_w: int, frame_h: int, target_ar: float,
    center_x: float, center_y: float, desired_h: float,
    overrides: Optional["ClipCropOverrides"] = None,
) -> Tuple[int, int, int, int]:
    if overrides:
        center_x += overrides.pan_x
        center_y += overrides.pan_y
        if overrides.zoom > 0:
            desired_h = desired_h / overrides.zoom
    ch = min(float(desired_h), float(frame_h))
    cw = ch * target_ar
    if cw > frame_w:
        cw = float(frame_w)
        ch = cw / target_ar
    x = center_x - cw / 2.0
    y = center_y - ch / 2.0
    x = max(0.0, min(x, frame_w - cw))
    y = max(0.0, min(y, frame_h - ch))
    cw_e = max(2, _even(cw))
    ch_e = max(2, _even(ch))
    x_e = max(0, _even(x))
    y_e = max(0, _even(y))
    if x_e + cw_e > frame_w:
        x_e = max(0, _even(frame_w - cw_e))
    if y_e + ch_e > frame_h:
        y_e = max(0, _even(frame_h - ch_e))
    return x_e, y_e, cw_e, ch_e


def get_video_dimensions(video_path: str) -> Tuple[int, int]:
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height", "-of", "json", video_path]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        return int(stream["width"]), int(stream["height"])
    except Exception as e:
        logger.warning(f"ffprobe failed: {e}. Defaulting to 1920x1080.")
        return 1920, 1080


def format_time_ass(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs >= 100:
        cs -= 100
        s += 1
    if s >= 60:
        s -= 60
        m += 1
    if m >= 60:
        m -= 60
        h += 1
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _word_chunks_from_segment(seg: Dict[str, Any], max_chars: int = 12) -> List[Dict[str, Any]]:
    """
    Yields 1-2 word subtitle chunks for karaoke-style fast captions.
    Uses real word timestamps when available; otherwise linearly interpolates
    over the segment so captions still flow per-word instead of per-sentence.
    """
    words = seg.get("words") or []
    if not words:
        text = (seg.get("text") or "").strip()
        if not text:
            return []
        tokens = text.split()
        if not tokens:
            return []
        s = float(seg.get("start", 0.0))
        e = float(seg.get("end", 0.0))
        dur = max(0.05, e - s)
        per = dur / len(tokens)
        words = [
            {"start": s + i * per, "end": s + (i + 1) * per, "word": tk}
            for i, tk in enumerate(tokens)
        ]

    chunks: List[Dict[str, Any]] = []
    i = 0
    while i < len(words):
        w = words[i]
        wt = (w.get("word") or "").strip()
        if not wt:
            i += 1
            continue
        if i + 1 < len(words):
            nxt = words[i + 1]
            nxt_t = (nxt.get("word") or "").strip()
            combined = f"{wt} {nxt_t}".strip() if nxt_t else wt
            if nxt_t and len(combined) <= max_chars:
                chunks.append({
                    "start": float(w.get("start", 0.0)),
                    "end": float(nxt.get("end", w.get("end", 0.0))),
                    "text": combined,
                })
                i += 2
                continue
        chunks.append({
            "start": float(w.get("start", 0.0)),
            "end": float(w.get("end", 0.0)),
            "text": wt,
        })
        i += 1
    return chunks


def _speaker_at(speaker_segments: Optional[List[Dict[str, Any]]], t: float) -> Optional[str]:
    if not speaker_segments:
        return None
    for s in speaker_segments:
        if s.get("start", 0.0) <= t < s.get("end", 0.0):
            return s.get("speaker", "UNKNOWN")
    return None


def generate_ass_file(
    segments: List[Dict[str, Any]],
    clip_start: float, clip_end: float,
    hook_text: str,
    output_path: str,
    speaker_segments: Optional[List[Dict[str, Any]]] = None,
    out_w: int = OUT_W, out_h: int = OUT_H,
    render_start: Optional[float] = None,
    caption_style: str = "bold_pop",
    silence_ranges_in_clip: Optional[List[List[float]]] = None,
    emphasis: Optional[List[Dict[str, Any]]] = None,
    hook_animate: bool = True,
) -> None:
    base = render_start if render_start is not None else clip_start
    style_cfg = _resolve_caption_style(caption_style)
    sub_cfg = style_cfg["subtitle"]
    hook_cfg = style_cfg["hook"]
    uppercase = bool(style_cfg.get("uppercase"))
    highlight_mode = bool(style_cfg.get("highlight_mode"))
    silences = silence_ranges_in_clip or []

    def rel_t(t_abs: float) -> float:
        """Convert absolute video time to clip-relative time post-silence-skip."""
        raw = t_abs - base
        if raw <= 0:
            return 0.0
        return max(0.0, raw - _shift_for_silence(t_abs, silences))

    # Emoji style: top-right corner, large, no outline. NotoColorEmoji is the
    # font name when fonts-noto-color-emoji is installed (see Dockerfile).
    emoji_cfg = {
        "font": "Noto Color Emoji", "size": 140,
        "primary": "&H00FFFFFF", "secondary": "&H000000FF",
        "outline_c": "&H00000000", "back": "&H00000000",
        "bold": 0, "outline": 0, "shadow": 0,
        "alignment": 9,  # top-right
        "margin_v": 60, "margin_l": 40, "margin_r": 40,
    }

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {out_w}",
        f"PlayResY: {out_h}",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        _ass_style_line("SubtitleStyle", sub_cfg),
        _ass_style_line("HookStyle", hook_cfg),
        _ass_style_line("EmojiStyle", emoji_cfg),
    ]

    # Per-speaker styles override only the primary color of the subtitle style.
    # In karaoke_highlight mode, this becomes the "fill" color words turn into.
    speaker_styles: Dict[str, str] = {}
    if speaker_segments:
        seen: Dict[str, str] = {}
        for s in speaker_segments:
            sp = s.get("speaker", "UNKNOWN")
            if sp not in seen:
                idx = len(seen) % len(SPEAKER_COLORS)
                seen[sp] = SPEAKER_COLORS[idx]
                style_name = f"Spkr{idx}"
                lines.append(_ass_style_line(style_name, sub_cfg, primary_override=SPEAKER_COLORS[idx]))
                speaker_styles[sp] = style_name

    lines.extend(["", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"])

    # Hook overlay: short, sits in lower-middle area above the subtitle line.
    if hook_text:
        hook_max = min(2.0, clip_end - clip_start)
        hook_min = min(1.2, hook_max)
        hook_duration = max(hook_min, min(hook_max, 0.06 * len(hook_text)))
        if hook_duration > 0.4:
            sanitized = hook_text.replace("{", "").replace("}", "").replace(chr(34), "").replace(chr(39), "")
            if uppercase:
                sanitized = sanitized.upper()
            # Scale-pop in (120%→100% in 280ms) then settle. Disabled for minimal style.
            anim_prefix = ""
            if hook_animate and caption_style != "minimal":
                anim_prefix = "{\\fscx120\\fscy120\\t(0,280,\\fscx100\\fscy100)}"
            lines.append(
                f"Dialogue: 1,{format_time_ass(0.0)},{format_time_ass(hook_duration)},HookStyle,,0,0,0,,{anim_prefix}{sanitized}"
            )

    # Emphasis emoji overlays: appear briefly at moments of surprise/punchline.
    # Position alternates left/right corner so multiple don't pile up.
    if emphasis:
        for i, e in enumerate(emphasis):
            try:
                t_abs = float(e.get("t", -1))
            except (TypeError, ValueError):
                continue
            emoji = str(e.get("emoji", "")).strip()
            if not emoji or t_abs < clip_start or t_abs > clip_end:
                continue
            rs = rel_t(t_abs)
            re_ = rel_t(t_abs + 1.0)
            if re_ - rs < 0.2:
                continue
            # Alternate corners: even index → top-right (default), odd → top-left
            pos_tag = ""
            if i % 2 == 1:
                # Shift to top-left by setting alignment override + position
                pos_tag = "{\\an7}"
            # Pop animation: scale 60% → 110% in 200ms, then 110% → 100% in 150ms
            anim = "{\\fscx60\\fscy60\\t(0,200,\\fscx110\\fscy110)\\t(200,350,\\fscx100\\fscy100)}"
            lines.append(
                f"Dialogue: 2,{format_time_ass(rs)},{format_time_ass(re_)},EmojiStyle,,0,0,0,,{pos_tag}{anim}{emoji}"
            )

    def _format_word_text(t: str) -> str:
        t = t.strip().replace("{", "").replace("}", "")
        return t.upper() if uppercase else t

    def _resolve_style_for_time(t_mid: float) -> Optional[str]:
        """Returns the style name to use, or None if word should be skipped (active speaker mode)."""
        if speaker_segments is None:
            return "SubtitleStyle"
        sp = _speaker_at(speaker_segments, t_mid)
        if sp is None:
            return None
        return speaker_styles.get(sp, "SubtitleStyle")

    if highlight_mode:
        # Karaoke phrases — group word chunks per segment, emit one Dialogue per phrase
        # with \k tags per chunk for word-by-word color fill.
        max_chunks = int(style_cfg.get("phrase_max_chunks", 4))
        max_chars = int(style_cfg.get("phrase_max_chars", 30))
        for seg in segments:
            seg_start = float(seg.get("start", 0.0))
            seg_end = float(seg.get("end", 0.0))
            if seg_end <= clip_start or seg_start >= clip_end:
                continue
            seg_chunks = [
                c for c in _word_chunks_from_segment(seg)
                if c["end"] > clip_start and c["start"] < clip_end
            ]
            if not seg_chunks:
                continue
            for phrase in _group_chunks_into_phrases(seg_chunks, max_chunks=max_chunks, max_chars=max_chars):
                p_start = phrase[0]["start"]
                p_end = phrase[-1]["end"]
                rs = rel_t(p_start)
                re_ = rel_t(p_end)
                if re_ - rs < 0.05:
                    continue
                style = _resolve_style_for_time((p_start + p_end) / 2.0)
                if style is None:
                    continue
                parts = []
                for c in phrase:
                    k_cs = max(1, int(round((c["end"] - c["start"]) * 100)))
                    parts.append(f"{{\\k{k_cs}}}{_format_word_text(c['text'])}")
                karaoke_text = " ".join(parts)
                lines.append(
                    f"Dialogue: 0,{format_time_ass(rs)},{format_time_ass(re_)},{style},,0,0,0,,{karaoke_text}"
                )
    else:
        # Per-chunk emission (1-2 word pops, one Dialogue each)
        for seg in segments:
            seg_start = float(seg.get("start", 0.0))
            seg_end = float(seg.get("end", 0.0))
            if seg_end <= clip_start or seg_start >= clip_end:
                continue
            for chunk in _word_chunks_from_segment(seg):
                c_start = chunk["start"]
                c_end = chunk["end"]
                if c_end <= clip_start or c_start >= clip_end:
                    continue
                rs = rel_t(c_start)
                re_ = rel_t(c_end)
                if re_ - rs < 0.05:
                    continue
                text = _format_word_text(chunk["text"])
                if not text:
                    continue
                style = _resolve_style_for_time((c_start + c_end) / 2.0)
                if style is None:
                    continue
                lines.append(
                    f"Dialogue: 0,{format_time_ass(rs)},{format_time_ass(re_)},{style},,0,0,0,,{text}"
                )

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    logger.info(f"Generated ASS [{caption_style}]: {output_path}")


# --- Filter builders ---

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
    """
    Builds an ffmpeg expression evaluating a piecewise-linear function of `t`
    over the given (t, value) knots. Before the first knot returns value0;
    after the last knot returns valueN-1.
    """
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
    """
    Builds a face-track filter where the crop window follows the subject across
    sampled positions instead of staying static. Returns None if the trajectory
    is unusable (too few points or degenerate boxes), so the caller can fall
    back to the static box.
    """
    if not trajectory or len(trajectory) < 2:
        return None

    # Compute a crop box per sample. Crop dimensions are fixed (median) so the
    # output frame size is stable; only x and y vary over time.
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

    # Recompute x, y for each knot using median dimensions so the framing is
    # consistent. Clamp into valid bounds.
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
        # Use clip-relative time (ffmpeg's t after pre-seek starts at 0)
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
    """
    Stack speaker crops top-to-bottom (vstack). Each panel is out_w wide and
    ~out_h/n tall, so a face fills the frame naturally instead of being squeezed
    into a thin side-by-side strip. Panel heights sum to exactly out_h (any
    rounding remainder is absorbed by the last panel).
    """
    n = len(face_boxes)
    base_h = out_h // n
    base_h -= base_h % 2
    panel_hs = [base_h] * n
    panel_hs[-1] = out_h - base_h * (n - 1)  # absorb rounding into the last panel
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
    """
    Wraps the existing layout video-filter string into a -filter_complex graph
    that also skips silence ranges (in clip-relative seconds) on both video and
    audio streams. Returns (filter_complex, video_out_label, audio_out_label).
    """
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

    # Filter silence ranges to those overlapping this render window.
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
        # Try dynamic face tracking first; fall back to static box on failure.
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
        # No-op until BG music is added in a future phase. Kept as a stub so the
        # flag and form field stay backwards-compatible.
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

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
            stderr_out = proc.stderr.read() if proc.stderr else ""
            logger.error(f"FFmpeg exit {proc.returncode} for {clip.id}: {stderr_out[:300]}")
            raise RuntimeError(f"FFmpeg exited with code {proc.returncode}")
    except RuntimeError:
        raise
    except Exception as e:
        logger.error(f"FFmpeg render failed for {clip.id}: {e}")
        raise RuntimeError(f"FFmpeg render failed: {e}")

    if os.path.exists(ass_path):
        try: os.remove(ass_path)
        except: pass

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
    groups.append({"speaker": cur_sp, "segments": cur})

    # Merge groups < 2s with previous
    merged = []
    for g in groups:
        dur = g["segments"][-1]["end"] - g["segments"][0]["start"]
        if dur < 2.0 and merged:
            merged[-1]["segments"].extend(g["segments"])
        else:
            merged.append(g)
    return merged


def render_active_speaker_clip(
    video_path: str, clip: Clip, segments: List[Dict[str, Any]],
    job_dir: str, crop_w: int, crop_h: int, w: int, h: int,
    speaker_segments: List[Dict[str, Any]],
    all_face_boxes: List[Tuple[int, int, int, int]],
    aspect_ratio: str, audio_ducking: bool,
    progress_callback,
    caption_style: str = "bold_pop",
    crop_overrides: Optional[ClipCropOverrides] = None,
) -> str:
    from app.pipeline.face_detect import detect_face_camera
    from app.pipeline.layout_engine import get_layout_params

    groups = _group_speaker_segments(speaker_segments)
    clips_dir = os.path.join(job_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)
    sub_paths = []
    speaker_face_cache = {}
    total_sub = len(groups)

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

        out_w, out_h = _get_output_dims(aspect_ratio)
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
               "-i", video_path, "-vf", vf,
               "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
               "-c:v", "libx264", "-preset", "superfast", "-crf", "22",
               "-c:a", "aac", "-b:a", "192k"]
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
        # Fallback: return first sub-clip
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
    """
    Render a single clip end-to-end. Used by the per-clip rerender endpoint so
    the user can swap layout or caption style on one clip without rebuilding
    the whole job. Returns the output mp4 path.
    """
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

    caption_style = override_caption_style or job.caption_style or "bold_pop"
    layout_mode = override_layout or job.layout_mode

    crop_overrides = clip.crop_overrides

    # If the user forced a specific layout, never auto-switch to active-speaker.
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
        )

    clip.file_path = clip_path
    clip.download_url = f"/jobs/{job.id}/clips/{clip.id}"
    thumb_file = os.path.join(job_dir, "thumbnails", f"{clip.id}.jpg")
    if os.path.exists(thumb_file):
        clip.thumbnail_url = f"/jobs/{job.id}/clips/{clip.id}/thumb"
    return clip_path


# --- Main render entry point ---

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
            # Active speaker rendering for multi-speaker clips
            if multi_speaker and sp_segments and detect_faces:
                clip_path = render_active_speaker_clip(
                    video_path=video_path, clip=clip, segments=segments,
                    job_dir=job_dir, crop_w=crop_w, crop_h=crop_h,
                    w=w, h=h, speaker_segments=sp_segments,
                    all_face_boxes=face_boxes,
                    aspect_ratio=aspect_ratio, audio_ducking=job.audio_ducking or False,
                    progress_callback=_progress,
                    caption_style=job.caption_style or "bold_pop",
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
