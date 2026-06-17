import os
import json
import logging
import subprocess
import cv2
import numpy as np
from typing import Tuple, Optional, List

logger = logging.getLogger("simbioclip.pipeline.face_detect")


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
        logger.warning(f"ffprobe failed for face detection: {e}")
        return None


def _extract_frame(video_path: str, timestamp: float, target_w: int) -> Optional[np.ndarray]:
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


def detect_face_camera(
    video_path: str,
    start: Optional[float] = None,
    end: Optional[float] = None,
    sample_count: Optional[int] = None,
) -> Optional[Tuple[int, int, int, int]]:
    if not os.path.exists(video_path):
        logger.warning(f"Video file not found for face detection: {video_path}")
        return None

    cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    profile_path = cv2.data.haarcascades + "haarcascade_profileface.xml"

    face_cascade = cv2.CascadeClassifier(cascade_path)
    profile_cascade = cv2.CascadeClassifier(profile_path)

    if face_cascade.empty():
        logger.warning("Failed to load frontal face Haar Cascade classifier.")
        return None

    probe = _probe_video(video_path)
    if probe is None:
        return None
    width, height, duration = probe

    if width <= 0 or height <= 0 or duration <= 0:
        logger.warning(f"Invalid video properties (w={width}, h={height}, dur={duration}s).")
        return None

    w_start = 0.0 if start is None else max(0.0, float(start))
    w_end = duration if end is None else min(duration, float(end))
    if w_end <= w_start:
        w_start, w_end = 0.0, duration
    window = w_end - w_start

    whole_video = start is None and end is None

    if sample_count is None:
        sample_count = 30 if whole_video else max(10, min(18, int(window / 2.5)))

    logger.info(
        f"Analyzing {video_path} ({width}x{height}) for face camera "
        f"in window {w_start:.1f}s–{w_end:.1f}s ({sample_count} samples)..."
    )

    timestamps = [w_start + window * (i + 0.5) / sample_count for i in range(sample_count)]

    target_w = min(width, 960)
    scale = width / target_w

    min_face = max(20, int(target_w * 0.025))
    det_kwargs = dict(scaleFactor=1.12, minNeighbors=5, minSize=(min_face, min_face))

    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))

    def _detect(gray_img):
        iw = gray_img.shape[1]
        enhanced = clahe.apply(gray_img)

        boxes = [tuple(b) for b in face_cascade.detectMultiScale(enhanced, **det_kwargs)]

        if len(boxes) == 0:
            boxes = [tuple(b) for b in face_cascade.detectMultiScale(gray_img, **det_kwargs)]

        if profile_cascade and not profile_cascade.empty():
            for (px, py, pw, ph) in profile_cascade.detectMultiScale(enhanced, **det_kwargs):
                boxes.append((px, py, pw, ph))
            flipped = cv2.flip(enhanced, 1)
            for (px, py, pw, ph) in profile_cascade.detectMultiScale(flipped, **det_kwargs):
                boxes.append((iw - (px + pw), py, pw, ph))

        return boxes

    detections = []
    decoded = 0

    for ts in timestamps:
        frame = _extract_frame(video_path, ts, target_w)
        if frame is None:
            continue
        decoded += 1

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        frame_boxes = []
        for (x, y, bw, bh) in _detect(gray):
            cx, cy = x + bw / 2, y + bh / 2
            if any(abs(cx - (ex + ew / 2)) < 20 and abs(cy - (ey + eh / 2)) < 20
                   for (ex, ey, ew, eh) in frame_boxes):
                continue
            frame_boxes.append((x, y, bw, bh))

        if frame_boxes:
            logger.debug(f"Sample @{ts:.0f}s: found {len(frame_boxes)} candidate face(s)")

        for (x, y, bw, bh) in frame_boxes:
            detections.append((int(x * scale), int(y * scale), int(bw * scale), int(bh * scale)))

    logger.info(
        f"Frame sampling done: decoded {decoded}/{sample_count} frames, "
        f"found {len(detections)} candidate face(s)."
    )
    if decoded == 0:
        logger.warning("ffmpeg decoded 0 frames for face detection.")

    if not detections:
        logger.info("No face camera detected (0 faces found).")
        return None

    best_cluster: List[Tuple[int, int, int, int]] = []
    max_cluster_size = 0

    tol_x = width * 0.10
    tol_y = height * 0.10

    for i, det1 in enumerate(detections):
        x1, y1, w1, h1 = det1
        cx1, cy1 = x1 + w1/2, y1 + h1/2

        current_cluster = [det1]
        for j, det2 in enumerate(detections):
            if i == j:
                continue
            x2, y2, w2, h2 = det2
            cx2, cy2 = x2 + w2/2, y2 + h2/2

            if abs(cx1 - cx2) < tol_x and abs(cy1 - cy2) < tol_y:
                current_cluster.append(det2)

        if len(current_cluster) > max_cluster_size:
            max_cluster_size = len(current_cluster)
            best_cluster = current_cluster

    required_consensus = max(3, int(round(sample_count * 0.1))) if whole_video else 2

    if max_cluster_size >= required_consensus:
        xs = [d[0] for d in best_cluster]
        ys = [d[1] for d in best_cluster]
        ws = [d[2] for d in best_cluster]
        hs = [d[3] for d in best_cluster]

        consensus_box = (
            int(np.median(xs)),
            int(np.median(ys)),
            int(np.median(ws)),
            int(np.median(hs))
        )
        logger.info(f"Consensus face camera located at: {consensus_box} (detected in {max_cluster_size} frames)")
        return consensus_box

    logger.info(f"No consistent face camera found. Best cluster size: {max_cluster_size} (requires {required_consensus}).")
    return None


def detect_all_faces(
    video_path: str,
    start: Optional[float] = None,
    end: Optional[float] = None,
    sample_count: Optional[int] = None,
) -> List[Tuple[int, int, int, int]]:
    """
    Returns ALL consistent face clusters found in the video.
    Used for multi-speaker content (podcasts, interviews).
    """
    if not os.path.exists(video_path):
        return []

    probe = _probe_video(video_path)
    if probe is None:
        return []
    width, height, duration = probe
    if width <= 0 or height <= 0 or duration <= 0:
        return []

    w_start = 0.0 if start is None else max(0.0, float(start))
    w_end = duration if end is None else min(duration, float(end))
    if w_end <= w_start:
        w_start, w_end = 0.0, duration
    window = w_end - w_start
    whole_video = start is None and end is None

    if sample_count is None:
        sample_count = 30 if whole_video else max(12, min(20, int(window / 2)))

    timestamps = [w_start + window * (i + 0.5) / sample_count for i in range(sample_count)]

    target_w = min(width, 960)
    scale = width / target_w

    cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    profile_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")
    if cascade.empty():
        return []

    min_face = max(20, int(target_w * 0.025))
    det_kwargs = dict(scaleFactor=1.12, minNeighbors=5, minSize=(min_face, min_face))
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))

    detections: List[Tuple[int, int, int, int]] = []

    for ts in timestamps:
        frame = _extract_frame(video_path, ts, target_w)
        if frame is None:
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        enhanced = clahe.apply(gray)

        boxes = cascade.detectMultiScale(enhanced, **det_kwargs)
        if len(boxes) == 0:
            boxes = cascade.detectMultiScale(gray, **det_kwargs)

        if profile_cascade and not profile_cascade.empty():
            for (px, py, pw, ph) in profile_cascade.detectMultiScale(enhanced, **det_kwargs):
                boxes = np.vstack([boxes, [[px, py, pw, ph]]]) if len(boxes) > 0 else np.array([[px, py, pw, ph]])
            flipped = cv2.flip(enhanced, 1)
            for (px, py, pw, ph) in profile_cascade.detectMultiScale(flipped, **det_kwargs):
                iw = enhanced.shape[1]
                boxes = np.vstack([boxes, [[iw - (px + pw), py, pw, ph]]]) if len(boxes) > 0 else np.array([[iw - (px + pw), py, pw, ph]])

        for (fx, fy, fw, fh) in boxes:
            orig = (int(fx * scale), int(fy * scale), int(fw * scale), int(fh * scale))
            if orig[2] > width * 0.5 or orig[3] > height * 0.5:
                continue

            cx, cy = orig[0] + orig[2] / 2, orig[1] + orig[3] / 2
            if any(abs(cx - (ex + ew / 2)) < 20 and abs(cy - (ey + eh / 2)) < 20
                   for (ex, ey, ew, eh) in detections):
                continue
            detections.append(orig)

    if not detections:
        return []

    tol_x = width * 0.08
    tol_y = height * 0.08
    clusters: List[List[Tuple[int, int, int, int]]] = []

    for det in detections:
        cx1, cy1 = det[0] + det[2] / 2, det[1] + det[3] / 2
        added = False
        for cluster in clusters:
            rep = cluster[0]
            cx2, cy2 = rep[0] + rep[2] / 2, rep[1] + rep[3] / 2
            if abs(cx1 - cx2) < tol_x and abs(cy1 - cy2) < tol_y:
                cluster.append(det)
                added = True
                break
        if not added:
            clusters.append([det])

    required = max(3, int(sample_count * 0.08)) if whole_video else 2
    result = []
    for cluster in clusters:
        if len(cluster) >= required:
            xs = [d[0] for d in cluster]
            ys = [d[1] for d in cluster]
            ws = [d[2] for d in cluster]
            hs = [d[3] for d in cluster]
            face = (
                int(np.median(xs)),
                int(np.median(ys)),
                int(np.median(ws)),
                int(np.median(hs)),
            )
            result.append(face)

    logger.info(f"detect_all_faces: found {len(result)} consistent face(s) in window {w_start:.1f}s–{w_end:.1f}s")
    return result


def detect_face_trajectory(
    video_path: str,
    start: float,
    end: float,
    num_samples: int = 5,
) -> List[Tuple[float, int, int, int, int]]:
    """
    Samples face positions across the clip window to enable a camera that
    follows the subject. Returns a list of (t_abs, fx, fy, fw, fh) sorted by t.
    Returns an empty list for short clips or when faces can't be detected.

    Cheap pass: at each sample point we run detect_face_camera with a narrow
    sub-window (~3 frames). Total decodes ≈ num_samples * 3.
    """
    clip_dur = end - start
    if clip_dur < 4.0 or num_samples < 2:
        return []

    interval = clip_dur / num_samples
    trajectory: List[Tuple[float, int, int, int, int]] = []
    for i in range(num_samples):
        center = start + interval * (i + 0.5)
        half = max(0.5, interval * 0.4)
        sub_start = max(start, center - half)
        sub_end = min(end, center + half)
        try:
            box = detect_face_camera(video_path, sub_start, sub_end, sample_count=3)
        except Exception as e:
            logger.debug(f"trajectory sample at t={center:.1f}s failed: {e}")
            box = None
        if box:
            x, y, w, h = box
            trajectory.append((round(center, 2), int(x), int(y), int(w), int(h)))

    if len(trajectory) < 2:
        return []
    return trajectory
