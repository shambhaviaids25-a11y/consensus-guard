
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


def create_app() -> FastAPI:
    load_dotenv(Path(__file__).resolve().with_name(".env"))

    from backend.routes.audio_routes import router as audio_router
    from backend.routes.enrollment import (
        log_speaker_mapping_configuration,
        router as enrollment_router,
    )
    from backend.routes.video_routes import router as video_router
    from backend.routes.meeting_routes import router as meeting_router

    log_speaker_mapping_configuration()

    application = FastAPI(title="ConsensusGuard API")

    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.include_router(video_router)
    application.include_router(meeting_router)
    application.include_router(enrollment_router)
    application.include_router(audio_router)

    return application


app = create_app()


@app.get("/health")
def health_check():
    return {"status": "ok"}
