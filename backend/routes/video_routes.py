import cv2
import numpy as np
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.concurrency import run_in_threadpool
from services.video_service import validate_video_metadata, analyze_video_bytes, detector

router = APIRouter(prefix="/api/verify", tags=["Video Verification"])

VALID_ROLES = {"CFO", "COO", "VP", "HOL", "VP_SECURITY", "LEGAL"}
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


def analyze_image_snapshot(image_bytes: bytes, filename: str, role: str) -> dict:
    """Analyze a single live canvas frame (.jpg/.png) using detector.predict_frames."""
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        return {
            "role": role,
            "filename": filename,
            "verdict": "inconclusive",
            "confidence": 0.0,
            "reason": "Could not decode image frame.",
        }

    prediction = detector.predict_frames([frame],role=role)
    return {
        "role": role,
        "filename": filename,
        "verdict": prediction["status"],
        "confidence": prediction["confidence"],
        "frames_checked": prediction["frames_checked"],
        "suspicious_frames": prediction["suspicious_frames"],
        "reason": prediction["reason"],
    }


@router.post("/video")
async def verify_video(
    role: str = Form(..., description="Executive role: CFO, COO, VP, HOL, VP_SECURITY, or LEGAL"),
    file: UploadFile = File(..., description="Video file or JPEG/PNG frame for visual authenticity check"),
):
    normalized_role = role.strip().upper()
    if normalized_role not in VALID_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role '{role}'. Must be one of: {', '.join(sorted(VALID_ROLES))}.",
        )

    file_bytes = await file.read()
    fname = (file.filename or "upload.mp4").lower()

    if fname.endswith(IMAGE_EXTENSIONS) or (file.content_type or "").startswith("image/"):
        return await run_in_threadpool(
            analyze_image_snapshot,
            image_bytes=file_bytes,
            filename=file.filename or "frame.jpg",
            role=normalized_role,
        )

    validation_error = validate_video_metadata(
        filename=file.filename or "",
        content_type=file.content_type or "",
        size_bytes=len(file_bytes),
    )
    if validation_error:
        raise HTTPException(status_code=400, detail=validation_error)

    return await run_in_threadpool(
        analyze_video_bytes,
        video_bytes=file_bytes,
        filename=file.filename or "upload.mp4",
        role=normalized_role,
    )