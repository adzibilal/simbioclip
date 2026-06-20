import logging
from typing import Tuple, Optional, Dict, Any, List

logger = logging.getLogger("simbioclip.pipeline.layout_engine")

OUT_W = 1080
OUT_H = 1920

CONTENT_TYPE_LAYOUTS = {
    "game_stream": {
        "type": "split_cam",
        "facecam_panel_ratio": 0.28,
        "facecam_scale": 3.5,
        "label": "Game + Facecam",
    },
    "podcast": {
        "type": "podcast_split",
        "label": "Podcast",
    },
    "talking_head": {
        "type": "face_track",
        "facecam_panel_ratio": 1.0,
        "facecam_scale": 2.5,
        "label": "Talking Head",
    },
    "tutorial": {
        "type": "inset",
        "inset_size": 0.22,
        "inset_position": "bottom-right",
        "facecam_scale": 2.5,
        "label": "Tutorial",
    },
    "presentation": {
        "type": "inset",
        "inset_size": 0.20,
        "inset_position": "bottom-right",
        "facecam_scale": 2.5,
        "label": "Presentation",
    },
    "vlog": {
        "type": "face_track",
        "facecam_panel_ratio": 1.0,
        "facecam_scale": 2.5,
        "label": "Vlog",
    },
    "cinematic": {
        "type": "center_crop",
        "label": "Cinematic",
    },
    "vertical": {
        "type": "passthrough",
        "label": "Vertical",
    },
    "unknown": {
        "type": "center_crop",
        "label": "Standard",
    },
}


def get_layout_params(
    content_type: str,
    layout_mode: str,
    face_box: Optional[Tuple[int, int, int, int]],
    frame_w: int,
    frame_h: int,
    face_boxes: Optional[List[Tuple[int, int, int, int]]] = None,
    speaker_count: Optional[int] = None,
) -> Dict[str, Any]:
    effective_type = layout_mode if layout_mode != "auto" else content_type
    all_faces = face_boxes or ([face_box] if face_box else [])

    if effective_type == "center_crop":
        return {
            "type": "center_crop",
            "label": "Center Crop",
            "face_box": face_box,
            "face_boxes": all_faces,
        }

    # BG-Blur is a composition effect, not a face-driven crop — it needs no face
    # detection, so resolve it before the face-required branches below.
    if effective_type == "bg_blur":
        return {
            "type": "bg_blur",
            "label": "BG Blur",
        }

    if not all_faces and not face_box:
        if effective_type in ("vertical", "passthrough"):
            return {"type": "passthrough", "label": "Vertical Passthrough"}
        return {
            "type": "center_crop",
            "label": "Center Crop (no face)",
            "face_box": None,
            "face_boxes": [],
        }

    if effective_type == "vertical":
        return {"type": "passthrough", "label": "Vertical Passthrough"}

    config = CONTENT_TYPE_LAYOUTS.get(effective_type, CONTENT_TYPE_LAYOUTS["unknown"])

    if config["type"] == "podcast_split":
        return _podcast_layout(all_faces, frame_w, frame_h, speaker_count)

    primary_face = face_box or (all_faces[0] if all_faces else None)
    if not primary_face:
        return {"type": "center_crop", "label": "Center Crop", "face_box": None, "face_boxes": []}

    if config["type"] == "split_cam":
        facecam_panel_h = int(OUT_H * config["facecam_panel_ratio"])
        facecam_panel_h -= facecam_panel_h % 2
        gameplay_panel_h = OUT_H - facecam_panel_h

        return {
            "type": "split_cam",
            "facecam_panel_h": facecam_panel_h,
            "gameplay_panel_h": gameplay_panel_h,
            "facecam_scale": config["facecam_scale"],
            "face_box": primary_face,
            "face_boxes": all_faces,
            "label": config["label"],
        }

    if config["type"] == "inset":
        inset_w = int(OUT_W * config["inset_size"])
        inset_h = int(OUT_H * config["inset_size"])
        inset_w -= inset_w % 2
        inset_h -= inset_h % 2
        inset_w = max(inset_w, 180)
        inset_h = max(inset_h, 320)

        return {
            "type": "inset",
            "inset_w": inset_w,
            "inset_h": inset_h,
            "inset_position": config["inset_position"],
            "facecam_scale": config["facecam_scale"],
            "face_box": primary_face,
            "face_boxes": all_faces,
            "label": config["label"],
        }

    if config["type"] == "face_track":
        return {
            "type": "face_track",
            "facecam_scale": config["facecam_scale"],
            "face_box": primary_face,
            "face_boxes": all_faces,
            "label": config["label"],
        }

    return {
        "type": "center_crop",
        "label": "Center Crop",
        "face_box": face_box,
        "face_boxes": all_faces,
    }


# A vertical 9:16 frame can only show one or two faces legibly. Three or four
# side-by-side panels become ~270px-wide slivers that are impossible to watch,
# so we hard-cap the number of podcast panels here.
MAX_PODCAST_PANELS = 2


def _podcast_layout(
    face_boxes: List[Tuple[int, int, int, int]],
    frame_w: int,
    frame_h: int,
    speaker_count: Optional[int] = None,
) -> Dict[str, Any]:
    n = len(face_boxes)
    if n == 0:
        return {"type": "center_crop", "label": "Center Crop", "face_box": None, "face_boxes": []}

    # Decide how many speakers to actually show. Cap at two panels, and when
    # diarization reported a speaker count, trust it to override noisy face
    # detection (e.g. a single-speaker podcast where the mic / background got
    # mis-detected as extra faces and inflated the panel count).
    target = min(n, MAX_PODCAST_PANELS)
    if speaker_count and speaker_count >= 1:
        target = min(target, max(1, speaker_count))

    # Keep the most prominent faces. False positives (mic stands, posters, the
    # wall) are almost always smaller than the real speaker(s), so ranking by
    # area drops them before they can become a panel.
    faces_by_area = sorted(face_boxes, key=lambda b: b[2] * b[3], reverse=True)
    chosen = faces_by_area[:target]
    if n > len(chosen):
        logger.info(
            f"Podcast layout: {n} faces detected, showing the {len(chosen)} "
            f"largest (speaker_count={speaker_count})."
        )

    # Single speaker → clean full-frame face track. (The old split_cam fallback
    # duplicated the same scene into a second panel, which looked broken.)
    if len(chosen) <= 1:
        face = chosen[0] if chosen else face_boxes[0]
        return {
            "type": "face_track",
            "facecam_scale": 2.5,
            "face_box": face,
            "face_boxes": [face],
            "label": "Podcast (1 speaker)",
            "speaker_count": 1,
        }

    # Two speakers → stack top/bottom (vstack). Each panel is ~1080x960, which
    # frames a face far better than two thin 540-wide side-by-side strips.
    chosen_sorted = sorted(chosen, key=lambda b: b[0])
    panel_h = OUT_H // 2
    panel_h -= panel_h % 2
    return {
        "type": "podcast_stack",
        "panel_h": panel_h,
        "face_boxes": chosen_sorted,
        "facecam_scale": 2.2,
        "label": "Podcast (2 speakers)",
        "speaker_count": speaker_count or 2,
    }
