from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routes.video_routes import router as video_router
from routes.meeting_routes import router as meeting_router

app = FastAPI(title="ConsensusGuard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(video_router)
app.include_router(meeting_router)


@app.get("/health")
def health_check():
    return {"status": "ok"}