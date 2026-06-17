import os
import json
import logging
import subprocess
import cv2
import numpy as np
from typing import List, Tuple, Optional, Dict, Any

logger = logging.getLogger("simbioclip.pipeline.scene_classifier")


def _probe_video(video_path: str) -> Optional[Tuple[int, int, float]]:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,duration:format=duration",
        "-of", "json",
        video_path,
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        width = int(stream["width"])
        height = int(stream["height"])
        duration = float(data.get("format", {}).get("duration") or stream.get("duration") or 0.0)
        return width, height, duration
    except Exception as e:
        logger.warning(f"ffprobe failed for scene classifier: {e}")
        return None


def _extract_frame(video_path: str, timestamp: float, target_w: int = 640) -> Optional[np.ndarray]:
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-ss", f"{timestamp:.3f}",
        "-i", video_path,
        "-frames:v", "1",
        "-vf", f"scale={target_w}:-2",
        "-f", "image2pipe",
        "-vcodec", "png",
        "pipe:1",
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
    except subprocess.CalledProcessError:
        return None
    if not result.stdout:
        return None
    buf = np.frombuffer(result.stdout, dtype=np.uint8)
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def _estimate_motion(prev_gray: np.ndarray, curr_gray: np.ndarray) -> float:
    diff = cv2.absdiff(prev_gray, curr_gray)
    return float(np.mean(diff))


def _estimate_edge_density(gray: np.ndarray) -> float:
    edges = cv2.Canny(gray, 50, 150)
    return float(np.count_nonzero(edges)) / float(edges.size)


def _estimate_screen_content(gray: np.ndarray) -> float:
    edges = cv2.Canny(gray, 30, 100)
    h, w = gray.shape
    top_half = edges[:h//2, :]
    bottom_half = edges[h//2:, :]
    top_density = np.count_nonzero(top_half) / float(top_half.size)
    bottom_density = np.count_nonzero(bottom_half) / float(bottom_half.size)
    return max(top_density, bottom_density)


def classify_content_type(
    video_path: str,
    segments: Optional[List[Dict[str, Any]]] = None
) -> str:
    try:
        return _classify_internal(video_path, segments)
    except Exception as e:
        logger.error(f"Scene classification failed: {e}", exc_info=True)
        return "unknown"


def _classify_internal(
    video_path: str,
    segments: Optional[List[Dict[str, Any]]] = None
) -> str:
    if not os.path.exists(video_path):
        logger.warning("Video file not found for scene classification.")
        return "unknown"

    probe = _probe_video(video_path)
    if probe is None:
        return "unknown"

    width, height, duration = probe
    if width <= 0 or height <= 0 or duration <= 0:
        return "unknown"

    if width < height:
        logger.info("Video is vertical (portrait). Classifying as 'vertical'.")
        return "vertical"

    target_w = min(width, 640)

    sample_count = min(20, max(8, int(duration / 15)))
    timestamps = [duration * (i + 0.5) / sample_count for i in range(sample_count)]

    face_cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    profile_cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_profileface.xml"
    )

    min_face = max(20, int(target_w * 0.03))
    det_kwargs = dict(scaleFactor=1.15, minNeighbors=4, minSize=(min_face, min_face))

    face_areas: List[float] = []
    face_counts: List[int] = []
    motion_values: List[float] = []
    edge_densities: List[float] = []
    screen_scores: List[float] = []
    prev_gray: Optional[np.ndarray] = None

    for i, ts in enumerate(timestamps):
        frame = _extract_frame(video_path, ts, target_w)
        if frame is None:
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray_eq = cv2.equalizeHist(gray)

        if prev_gray is not None:
            motion_values.append(_estimate_motion(prev_gray, gray))
        prev_gray = gray

        edge_densities.append(_estimate_edge_density(gray))
        screen_scores.append(_estimate_screen_content(gray))

        faces = face_cascade.detectMultiScale(gray_eq, **det_kwargs)
        if profile_cascade and not profile_cascade.empty():
            profile_faces = profile_cascade.detectMultiScale(gray_eq, **det_kwargs)
            flipped = cv2.flip(gray_eq, 1)
            for (px, py, pw, ph) in profile_cascade.detectMultiScale(flipped, **det_kwargs):
                iw = gray_eq.shape[1]
                faces = np.vstack([faces, [[iw - (px + pw), py, pw, ph]]]) if len(faces) > 0 else \
                        np.array([[iw - (px + pw), py, pw, ph]])
            if len(profile_faces) > 0:
                faces = np.vstack([faces, profile_faces]) if len(faces) > 0 else profile_faces

        if len(faces) > 0:
            face_counts.append(len(faces))
            for (fx, fy, fw, fh) in faces:
                area = (fw * fh) / float(target_w * (target_w * height / width))
                face_areas.append(area)
        else:
            face_counts.append(0)

    if not motion_values and not face_counts:
        return "unknown"

    avg_motion = np.mean(motion_values) if motion_values else 0.0
    avg_edge = np.mean(edge_densities) if edge_densities else 0.0
    avg_screen = np.mean(screen_scores) if screen_scores else 0.0
    avg_face_count = np.mean(face_counts) if face_counts else 0.0
    avg_face_area = np.mean(face_areas) if face_areas else 0.0
    max_face_area = max(face_areas) if face_areas else 0.0
    motion_std = np.std(motion_values) if motion_values else 0.0

    logger.info(
        f"Scene features — faces:{avg_face_count:.1f} "
        f"face_area:{avg_face_area:.3f} "
        f"max_face_area:{max_face_area:.3f} "
        f"motion:{avg_motion:.2f}±{motion_std:.2f} "
        f"edge:{avg_edge:.4f} "
        f"screen:{avg_screen:.4f}"
    )

    content_type = _heuristic_classify(
        avg_face_count, avg_face_area, max_face_area,
        avg_motion, motion_std, avg_screen, width, height
    )

    logger.info(f"Classified content as: {content_type}")
    return content_type


def _heuristic_classify(
    avg_face_count: float,
    avg_face_area: float,
    max_face_area: float,
    avg_motion: float,
    motion_std: float,
    screen_score: float,
    width: int,
    height: int,
) -> str:
    ultra_wide = width / height > 2.0

    if ultra_wide and avg_face_area < 0.08 and avg_face_count <= 1:
        return "game_stream"

    if avg_face_count >= 2.5:
        return "podcast"

    if screen_score > 0.12 and avg_face_area < 0.08:
        if avg_motion < 8.0:
            return "presentation"
        return "tutorial"

    if screen_score > 0.08 and avg_motion < 5.0 and avg_face_area < 0.1:
        return "presentation"

    if avg_face_area > 0.15:
        return "talking_head"

    if avg_face_count >= 1.5:
        return "podcast"

    if avg_face_area < 0.05 and avg_motion > 10.0 and motion_std > 5.0:
        if screen_score > 0.06:
            return "tutorial"
        return "game_stream"

    if avg_face_count >= 0.5 and avg_face_area > 0.05:
        if avg_face_area > 0.12:
            return "talking_head"
        return "vlog"

    if avg_motion > 15.0:
        return "cinematic"

    if avg_face_area < 0.03 and screen_score > 0.05:
        return "tutorial"

    if avg_face_count >= 0.3:
        return "vlog"

    return "unknown"
