from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from services.video_service import (
    validate_video_metadata,
    analyze_video_bytes,
    analyze_frame_bytes,
    detector,
)

router = APIRouter(prefix="/api/verify", tags=["video"])


@router.post("/video")
async def verify_video(
    file: UploadFile = File(...),
    role: str = Form("DEFAULT"),
):
    raw_bytes = await file.read()
    validation = validate_video_metadata(
        filename=file.filename,
        content_type=file.content_type,
        size_bytes=len(raw_bytes),
    )
    if not validation.get("valid"):
        raise HTTPException(status_code=400, detail=validation.get("error", "Invalid media file."))

    content_type = (file.content_type or "").lower()
    if content_type.startswith("image/"):
        result = await run_in_threadpool(analyze_frame_bytes, raw_bytes, role)
    else:
        result = await run_in_threadpool(
            analyze_video_bytes,
            raw_bytes,
            file.filename or "segment.webm",
            role,
        )

    return {
        "verdict": result.get("verdict", "inconclusive"),
        "status": result.get("status", result.get("verdict", "inconclusive")),
        "confidence": result.get("confidence", 0.0),
        "frames_checked": result.get("frames_checked", result.get("frames_analyzed", 0)),
        "reason": result.get("reason", ""),
        "role": role,
        "model_available": detector.is_available,
    }