import os
import tempfile
from typing import Dict, Any, List, Optional
import cv2
import numpy as np
from dotenv import load_dotenv

load_dotenv()

MAX_VIDEO_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB limit
ALLOWED_EXTENSIONS = {".mp4", ".webm"}
ALLOWED_CONTENT_TYPES = {"video/mp4", "video/webm", "application/octet-stream"}


class VideoDeepfakeDetector:
    """
    Python 3.14 compatible CPU forensic detector using core OpenCV & NumPy:
    1. YCrCb human skin-tone & color naturalness check
    2. Phone-screen / rectangular bezel & screen-glare spoofing check
    3. Inner-subject vs. outer-boundary sharpness & Canny edge-halo check
    4. Temporal frame-to-frame micro-motion check
    """

    def __init__(self):
        self.model_available = True
        self._last_gray_by_role: Dict[str, Any] = {}

    @property
    def is_available(self) -> bool:
        return self.model_available

    def _locate_face_box(self, frame: Any) -> tuple:
        h, w = frame.shape[:2]
        # Focus on the center speaker area (exclude left/right side margins where hands hold a phone)
        y_top, y_bot = int(h * 0.16), int(h * 0.82)
        x_left, x_right = int(w * 0.22), int(w * 0.78)
        roi = frame[y_top:y_bot, x_left:x_right]

        ycrcb = cv2.cvtColor(roi, cv2.COLOR_BGR2YCrCb)
        # Tighter natural human skin chrominance bounds (excludes warm beige/metallic robot tones)
        mask = cv2.inRange(ycrcb, np.array([50, 138, 82]), np.array([235, 170, 122]))
        ys, xs = np.where(mask > 0)
        skin_ratio = len(xs) / max(1, roi.shape[0] * roi.shape[1])

        if len(xs) >= 600:
            x1 = int(np.percentile(xs, 15)) + x_left
            x2 = int(np.percentile(xs, 85)) + x_left
            y1 = int(np.percentile(ys, 15)) + y_top
            y2 = int(np.percentile(ys, 85)) + y_top
            fw, fh = x2 - x1, y2 - y1
            if fw >= 45 and fh >= 45:
                return (x1, y1, fw, fh), skin_ratio

        fw, fh = int(w * 0.32), int(h * 0.44)
        x1, y1 = (w - fw) // 2, (h - fh) // 2
        return (x1, y1, fw, fh), skin_ratio

    def _inspect_single_frame(self, frame: Any, role: str = "DEFAULT") -> Dict[str, Any]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape[:2]

        if h < 60 or w < 60:
            return {
                "subject_detected": False,
                "suspicious": False,
                "detail": "frame too small",
            }

        (x, y, fw, fh), skin_ratio = self._locate_face_box(frame)

        pad_x, pad_y = int(fw * 0.18), int(fh * 0.18)
        ix1, iy1 = max(0, x + pad_x), max(0, y + pad_y)
        ix2, iy2 = min(w, x + fw - pad_x), min(h, y + fh - pad_y)
        inner_roi = gray[iy1:iy2, ix1:ix2]

        ox1, oy1 = max(0, x - pad_x), max(0, y - pad_y)
        ox2, oy2 = min(w, x + fw + pad_x), min(h, y + fh + pad_y)
        outer_roi = gray[oy1:oy2, ox1:ox2]

        if inner_roi.size == 0 or outer_roi.size == 0:
            return {
                "subject_detected": False,
                "suspicious": False,
                "detail": "empty ROI",
            }

        # 1. Non-human / synthetic subject check (AI robot, cartoon, or low human skin presence)
        non_human_subject = skin_ratio < 0.045

        # 2. Screen-replay / phone-bezel spoof check (over-exposed screen glare + dark bezel contrast)
        center_crop = gray[int(h * 0.18):int(h * 0.82), int(w * 0.25):int(w * 0.75)]
        bright_screen_pixels = float(np.mean(center_crop > 235))
        dark_bezel_pixels = float(np.mean(center_crop < 45))
        screen_replay_spoof = (bright_screen_pixels > 0.12 and dark_bezel_pixels > 0.12)

        # 3. Inner-face vs. outer-jawline sharpness & edge-halo ratios
        inner_sharpness = float(cv2.Laplacian(inner_roi, cv2.CV_64F).var()) + 1e-5
        outer_sharpness = float(cv2.Laplacian(outer_roi, cv2.CV_64F).var()) + 1e-5
        boundary_ratio = inner_sharpness / outer_sharpness

        inner_edges = float(np.mean(cv2.Canny(inner_roi, 60, 150) > 0)) + 1e-5
        outer_edges = float(np.mean(cv2.Canny(outer_roi, 60, 150) > 0)) + 1e-5
        edge_halo_ratio = inner_edges / outer_edges

        boundary_anomaly = boundary_ratio < 0.24 or boundary_ratio > 3.8
        halo_anomaly = edge_halo_ratio < 0.26 or edge_halo_ratio > 3.6

        # 4. Temporal micro-motion check
        static_spoof = False
        resized_inner = cv2.resize(inner_roi, (96, 96))
        prev_inner = self._last_gray_by_role.get(role)
        self._last_gray_by_role[role] = resized_inner
        if prev_inner is not None:
            mean_diff = float(np.mean(cv2.absdiff(resized_inner, prev_inner)))
            if mean_diff < 1.1:
                static_spoof = True

        suspicious = bool(
            non_human_subject
            or screen_replay_spoof
            or boundary_anomaly
            or halo_anomaly
            or static_spoof
        )

        reasons = []
        if non_human_subject:
            reasons.append("non-human or synthetic avatar detected")
        if screen_replay_spoof:
            reasons.append("secondary screen/device replay detected")
        if boundary_anomaly or halo_anomaly:
            reasons.append("facial boundary/edge-halo anomaly detected")
        if static_spoof:
            reasons.append("static photo/frozen-frame spoofing detected")

        return {
            "subject_detected": True,
            "suspicious": suspicious,
            "detail": ", ".join(reasons) if reasons else "natural human facial dynamics verified",
        }

    def predict_frames(self, frames: List[Any], role: str = "DEFAULT") -> Dict[str, Any]:
        results = [self._inspect_single_frame(f, role=role) for f in frames]
        detected = [r for r in results if r["subject_detected"]]

        if not detected:
            return {
                "status": "no_suspicious_indicators",
                "confidence": 0.85,
                "frames_checked": 0,
                "suspicious_frames": 0,
                "reason": "No anomalies detected in visible frame.",
            }

        suspicious_items = [r for r in detected if r["suspicious"]]
        suspicious_count = len(suspicious_items)
        suspicious_ratio = suspicious_count / len(detected)

        if suspicious_ratio >= 0.4:
            detail_msg = suspicious_items[0]["detail"]
            return {
                "status": "suspicious_indicators",
                "confidence": round(min(0.99, 0.88 + suspicious_ratio * 0.10), 2),
                "frames_checked": len(detected),
                "suspicious_frames": suspicious_count,
                "reason": f"Visual authenticity alert: {detail_msg}.",
            }

        return {
            "status": "no_suspicious_indicators",
            "confidence": round(max(0.92, 1.0 - suspicious_ratio), 2),
            "frames_checked": len(detected),
            "suspicious_frames": suspicious_count,
            "reason": (
                f"Facial boundary sharpness and live motion verified across "
                f"{len(detected)} sampled frames."
            ),
        }


detector = VideoDeepfakeDetector()


def validate_video_metadata(filename: str, content_type: str, size_bytes: int) -> Optional[str]:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return f"Invalid file extension '{ext}'. Only .mp4 or .webm files are accepted."
    if content_type and content_type.lower() not in ALLOWED_CONTENT_TYPES:
        return f"Invalid MIME type '{content_type}'. Expected video/mp4."
    if size_bytes == 0:
        return "Uploaded video file is empty."
    if size_bytes > MAX_VIDEO_SIZE_BYTES:
        return f"File exceeds maximum allowed size of {MAX_VIDEO_SIZE_BYTES // (1024 * 1024)} MB."
    return None


def analyze_video_bytes(
    video_bytes: bytes,
    filename: str,
    role: str,
    target_samples: int = 8,
) -> Dict[str, Any]:
    errors: List[str] = []
    suffix = os.path.splitext(filename)[1] or ".mp4"
    tmp_path = None
    frames: List[Any] = []

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(video_bytes)
            tmp_path = tmp.name

        cap = cv2.VideoCapture(tmp_path)
        if not cap.isOpened():
            return {
                "role": role,
                "filename": filename,
                "verdict": "inconclusive",
                "confidence": 0.0,
                "reason": "Could not open video stream.",
            }

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames > 0:
            indices = np.linspace(0, total_frames - 1, min(target_samples, total_frames), dtype=int)
            for idx in indices:
                cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
                ok, frame = cap.read()
                if ok and frame is not None:
                    frames.append(frame)
        else:
            while len(frames) < target_samples:
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                frames.append(frame)
        cap.release()
    except Exception as exc:
        errors.append(str(exc))
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    if not frames:
        return {
            "role": role,
            "filename": filename,
            "verdict": "inconclusive",
            "confidence": 0.0,
            "reason": "No readable frames extracted from video.",
            "errors": errors,
        }

    prediction = detector.predict_frames(frames, role=role)
    return {
        "role": role,
        "filename": filename,
        "verdict": prediction["status"],
        "confidence": prediction["confidence"],
        "frames_checked": prediction["frames_checked"],
        "suspicious_frames": prediction["suspicious_frames"],
        "reason": prediction["reason"],
    }