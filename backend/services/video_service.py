import os
import tempfile
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import cv2

MAX_VIDEO_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB limit
ALLOWED_EXTENSIONS = {".mp4"}
ALLOWED_CONTENT_TYPES = {"video/mp4", "application/octet-stream"}


class VideoDeepfakeDetector:
    """
    Wrapper for an optional pretrained video deepfake detection model.
    Returns unavailable when no model weights are configured instead of inventing scores.
    """

    def __init__(self, model_path: Optional[str] = None):
        self.model_path = model_path or os.getenv("VIDEO_DEEPFAKE_MODEL_PATH")
        self.model = None
        self.load_error: Optional[str] = None

        if self.model_path and os.path.exists(self.model_path):
            self.load_error = (
                "Model weights found, but inference graph is not configured in this prototype."
            )
        else:
            self.load_error = (
                "No pretrained video deepfake model weights configured (VIDEO_DEEPFAKE_MODEL_PATH unset)."
            )

    @property
    def is_available(self) -> bool:
        return self.model is not None

    def predict_frames(self, frames: List[Any]) -> Dict[str, Any]:
        if not self.is_available:
            return {
                "status": "unavailable",
                "confidence": None,
                "reason": self.load_error,
            }
        return {
            "status": "inconclusive",
            "confidence": None,
            "reason": "Pretrained model inference returned no conclusive score.",
        }


detector = VideoDeepfakeDetector()


def validate_video_metadata(filename: str, content_type: str, size_bytes: int) -> Optional[str]:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return f"Invalid file extension '{ext}'. Only .mp4 files are accepted."
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
    frame_metadata: List[Dict[str, Any]] = []
    extracted_frames: List[Any] = []
    temp_path: Optional[str] = None

    video_info: Dict[str, Any] = {
        "filename": filename,
        "role": role,
        "size_bytes": len(video_bytes),
        "total_frames": 0,
        "fps": 0.0,
        "duration_sec": 0.0,
        "resolution": None,
    }

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
            tmp.write(video_bytes)
            temp_path = tmp.name

        cap = cv2.VideoCapture(temp_path)
        if not cap.isOpened():
            errors.append("OpenCV could not open the uploaded MP4 container.")
            return {
                "role": role,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "verdict": "inconclusive",
                "model_available": detector.is_available,
                "reason": "Corrupted or unreadable MP4 video file.",
                "video_info": video_info,
                "frames_analyzed": [],
                "errors": errors,
            }

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        duration_sec = round(total_frames / fps, 2) if fps > 0 else 0.0

        video_info.update({
            "total_frames": total_frames,
            "fps": round(fps, 2),
            "duration_sec": duration_sec,
            "resolution": f"{width}x{height}",
        })

        if total_frames <= 0:
            errors.append("Video contains zero decodable frames.")
            cap.release()
            return {
                "role": role,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "verdict": "inconclusive",
                "model_available": detector.is_available,
                "reason": "No frames found in uploaded video.",
                "video_info": video_info,
                "frames_analyzed": [],
                "errors": errors,
            }

        sample_count = min(target_samples, total_frames)
        step = max(1, total_frames // sample_count)
        sample_indices = [i * step for i in range(sample_count) if i * step < total_frames]

        for idx in sample_indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            if not ok or frame is None:
                errors.append(f"Failed to decode frame at index {idx}.")
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            sharpness = round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 2)
            brightness = round(float(gray.mean()), 2)
            ts_sec = round(idx / fps, 2) if fps > 0 else 0.0

            extracted_frames.append(frame)
            frame_metadata.append({
                "frame_index": idx,
                "timestamp_sec": ts_sec,
                "sharpness_variance": sharpness,
                "mean_brightness": brightness,
            })

        cap.release()

        if not extracted_frames:
            return {
                "role": role,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "verdict": "inconclusive",
                "model_available": detector.is_available,
                "reason": "Frame extraction yielded no usable frames.",
                "video_info": video_info,
                "frames_analyzed": frame_metadata,
                "errors": errors,
            }

        prediction = detector.predict_frames(extracted_frames)

        return {
            "role": role,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "verdict": prediction["status"],
            "confidence": prediction["confidence"],
            "model_available": detector.is_available,
            "reason": prediction["reason"],
            "video_info": video_info,
            "frames_analyzed": frame_metadata,
            "errors": errors,
        }

    except Exception as exc:
        errors.append(f"Unexpected video processing error: {str(exc)}")
        return {
            "role": role,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "verdict": "inconclusive",
            "model_available": detector.is_available,
            "reason": "Internal error during video frame analysis.",
            "video_info": video_info,
            "frames_analyzed": frame_metadata,
            "errors": errors,
        }
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass