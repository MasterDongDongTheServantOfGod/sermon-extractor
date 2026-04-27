from dotenv import load_dotenv
load_dotenv(override=True)

import os
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.database import Base, engine
from app.routers import articles, channels, videos

Base.metadata.create_all(bind=engine)

os.makedirs("articles", exist_ok=True)
os.makedirs("data", exist_ok=True)

app = FastAPI(title="Sermon Extractor", version="1.0.0")

app.include_router(channels.router, prefix="/api/channels", tags=["Channels"])
app.include_router(videos.router, prefix="/api/videos", tags=["Videos"])
app.include_router(articles.router, prefix="/api/articles", tags=["Articles"])

app.mount("/articles", StaticFiles(directory="articles"), name="articles")


@app.get("/", include_in_schema=False)
def serve_admin():
    return FileResponse("frontend/index.html")
