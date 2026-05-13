from dotenv import load_dotenv
load_dotenv(override=False)

import json
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.database import Base, SessionLocal, engine
from app.routers import articles, channels, videos

Base.metadata.create_all(bind=engine)


def _seed_channels():
    seed_path = os.path.join("data", "channels_seed.json")
    if not os.path.exists(seed_path):
        return
    from app.models import Channel
    with open(seed_path, "r", encoding="utf-8") as f:
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


_seed_channels()

# Render used cold-start filesystem generation for /articles and
# data/articles-index.json. Vercel functions have ephemeral storage, so published
# articles are served dynamically from the database instead.

app = FastAPI(title="Sermon Extractor", version="1.0.0")

app.include_router(channels.router, prefix="/api/channels", tags=["Channels"])
app.include_router(videos.router, prefix="/api/videos", tags=["Videos"])
app.include_router(articles.router, prefix="/api/articles", tags=["Articles"])


@app.get("/health")
def health():
    return {"ok": True}


def _admin_response():
    response = FileResponse("public/index.html")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", include_in_schema=False)
def serve_admin():
    return _admin_response()


@app.get("/generate", include_in_schema=False)
def serve_generate_page():
    return _admin_response()
