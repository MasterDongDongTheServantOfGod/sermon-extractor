from dotenv import load_dotenv
load_dotenv(override=False)

import json
import os
import traceback
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from app.database import Base, SessionLocal, engine
from app.routers import articles, channels, videos

BASE_DIR = Path(__file__).resolve().parent
ADMIN_HTML_PATH = BASE_DIR / "admin.html"


def _seed_channels():
    seed_path = BASE_DIR.parent / "data" / "channels_seed.json"
    if not seed_path.exists():
        return

    from app.models import Channel

    with seed_path.open("r", encoding="utf-8") as f:
        seeds = json.load(f)

    db = SessionLocal()
    try:
        for s in seeds:
            exists = db.query(Channel).filter(Channel.channel_id == s["channel_id"]).first()
            if not exists:
                db.add(Channel(
                    pastor_name=s["pastor_name"],
                    channel_id=s["channel_id"],
                    channel_title=s.get("channel_title"),
                    is_active=True,
                ))
        db.commit()
    finally:
        db.close()


def init_database():
    Base.metadata.create_all(bind=engine)
    _seed_channels()


app = FastAPI(title="Sermon Extractor", version="1.0.0")

app.include_router(channels.router, prefix="/api/channels", tags=["Channels"])
app.include_router(videos.router, prefix="/api/videos", tags=["Videos"])
app.include_router(articles.router, prefix="/api/articles", tags=["Articles"])


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/admin/init-db")
def init_db_route(x_admin_token: str = Header(default="")):
    expected = os.getenv("ADMIN_INIT_TOKEN", "")

    if expected and x_admin_token != expected:
        raise HTTPException(status_code=403, detail="Invalid admin token")

    try:
        init_database()
        return {"ok": True, "message": "Database initialized"}
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(exc))


def _admin_response():
    if not ADMIN_HTML_PATH.exists():
        return JSONResponse(
            status_code=500,
            content={
                "error": "Admin HTML not found",
                "expected_path": str(ADMIN_HTML_PATH),
            },
        )

    response = FileResponse(str(ADMIN_HTML_PATH))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", include_in_schema=False)
def serve_admin():
    return _admin_response()


@app.get("/generate", include_in_schema=False)
def serve_generate_page():
    return _admin_response()
