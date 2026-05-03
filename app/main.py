from dotenv import load_dotenv
load_dotenv(override=False)

import json
import os
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.database import Base, engine, SessionLocal
from app.routers import articles, channels, videos
from app.services import transcript_extractor

Base.metadata.create_all(bind=engine)

os.makedirs("articles", exist_ok=True)
os.makedirs("data", exist_ok=True)

transcript_extractor.getYoutubeCookiesPath()


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


def _rebuild_articles():
    """Regenerate static HTML + articles-index.json from DB on cold start."""
    import json as _json
    from datetime import datetime as _dt
    from app.models import Article, Video, Channel
    from app.services.static_publisher import rebuild_article_files

    db = SessionLocal()
    try:
        arts = (
            db.query(Article)
            .filter(Article.status == "published", Article.html_path.isnot(None))
            .all()
        )
        if not arts:
            return

        index_entries = []
        for art in arts:
            parts = (art.html_path or "").split("/")
            if len(parts) < 3:
                continue
            dir_name = parts[2]

            # Skip if HTML already exists (local dev — files persist)
            if os.path.exists(os.path.join("articles", dir_name, "article.html")):
                meta_path = os.path.join("articles", dir_name, "metadata.json")
                if os.path.exists(meta_path):
                    with open(meta_path, "r", encoding="utf-8") as f:
                        index_entries.append(_json.load(f))
                continue

            video = db.query(Video).filter(Video.id == art.video_id).first() if art.video_id else None
            channel = db.query(Channel).filter(Channel.id == video.channel_id).first() if video else None

            tags = _json.loads(art.tags) if art.tags else []
            entry = rebuild_article_files(
                article_id=art.id,
                dir_name=dir_name,
                title=art.title or "",
                deck=art.deck or "",
                article_body=art.article_body or "",
                primary_scripture=art.primary_scripture or "",
                seo_title=art.seo_title or "",
                meta_description=art.meta_description or "",
                tags=tags,
                pastor_name=channel.pastor_name if channel else "Unknown",
                church_name=channel.channel_title if channel else "",
                sermon_title=video.title if video else "",
                video_url=f"https://www.youtube.com/watch?v={video.youtube_video_id}" if video else "",
                thumbnail_url=video.thumbnail_url if video else "",
                published_date=art.published_at.strftime("%B %d, %Y") if art.published_at else "",
                seo_score=art.seo_score or 0,
                risk_level=art.risk_level or "LOW",
            )
            index_entries.append(entry)

        index_path = os.path.join("data", "articles-index.json")
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                current_index = _json.load(f)
        except (FileNotFoundError, _json.JSONDecodeError):
            current_index = {}

        if "articles" in current_index and current_index.get("articles", []) == index_entries:
            return

        with open(index_path, "w", encoding="utf-8") as f:
            _json.dump({"articles": index_entries, "updated_at": _dt.utcnow().isoformat()}, f, indent=2, ensure_ascii=False)
            f.write("\n")
    finally:
        db.close()


_rebuild_articles()

app = FastAPI(title="Sermon Extractor", version="1.0.0")

app.include_router(channels.router, prefix="/api/channels", tags=["Channels"])
app.include_router(videos.router, prefix="/api/videos", tags=["Videos"])
app.include_router(articles.router, prefix="/api/articles", tags=["Articles"])

app.mount("/articles", StaticFiles(directory="articles"), name="articles")


def _admin_response():
    response = FileResponse("frontend/index.html")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", include_in_schema=False)
def serve_admin():
    return _admin_response()


@app.get("/generate", include_in_schema=False)
def serve_generate_page():
    return _admin_response()
