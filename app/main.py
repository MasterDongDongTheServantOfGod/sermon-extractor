from dotenv import load_dotenv
load_dotenv(override=True)

import json
import os
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.database import Base, engine, SessionLocal
from app.routers import articles, channels, videos

Base.metadata.create_all(bind=engine)

os.makedirs("articles", exist_ok=True)
os.makedirs("data", exist_ok=True)


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

app = FastAPI(title="Sermon Extractor", version="1.0.0")

app.include_router(channels.router, prefix="/api/channels", tags=["Channels"])
app.include_router(videos.router, prefix="/api/videos", tags=["Videos"])
app.include_router(articles.router, prefix="/api/articles", tags=["Articles"])

app.mount("/articles", StaticFiles(directory="articles"), name="articles")


@app.get("/", include_in_schema=False)
def serve_admin():
    return FileResponse("frontend/index.html")
