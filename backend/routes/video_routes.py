from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.concurrency import run_in_threadpool
from services.video_service import validate_video_metadata, analyze_video_bytes

router = APIRouter(prefix="/api/verify", tags=["Video Verification"])

VALID_ROLES = {"CFO", "VP_SECURITY", "LEGAL"}


@router.post("/video")
async def verify_video(
    role: str = Form(..., description="Executive role: CFO, VP_SECURITY, or LEGAL"),
    file: UploadFile = File(..., description="MP4 video file for visual authenticity check"),
):
    normalized_role = role.strip().upper()
    if normalized_role not in VALID_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role '{role}'. Must be one of: {', '.join(sorted(VALID_ROLES))}.",
        )

    video_bytes = await file.read()
    validation_error = validate_video_metadata(
        filename=file.filename or "",
        content_type=file.content_type or "",
        size_bytes=len(video_bytes),
    )
    if validation_error:
        raise HTTPException(status_code=400, detail=validation_error)

    result = await run_in_threadpool(
        analyze_video_bytes,
        video_bytes=video_bytes,
        filename=file.filename or "upload.mp4",
        role=normalized_role,
    )
    return result