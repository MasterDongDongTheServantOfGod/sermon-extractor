from sqlalchemy import Column, Integer, String, Boolean, Float, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database import Base


class Channel(Base):
    __tablename__ = "channels"

    id = Column(Integer, primary_key=True)
    pastor_name = Column(String, nullable=False)
    channel_id = Column(String, unique=True, nullable=False)
    channel_title = Column(String)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    videos = relationship("Video", back_populates="channel")


class Video(Base):
    __tablename__ = "videos"

    id = Column(Integer, primary_key=True)
    youtube_video_id = Column(String, unique=True, nullable=False)
    channel_id = Column(Integer, ForeignKey("channels.id"))
    title = Column(String)
    published_at = Column(DateTime)
    duration_seconds = Column(Integer)
    view_count = Column(Integer)
    comment_count = Column(Integer, default=0)
    thumbnail_url = Column(String)
    transcript_status = Column(String, default="pending")  # pending, available, failed, whisper_needed
    transcript_type = Column(String)  # manual, auto, fallback
    score = Column(Float, default=0.0)
    failure_reason = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    channel = relationship("Channel", back_populates="videos")
    articles = relationship("Article", back_populates="video")


class Article(Base):
    __tablename__ = "articles"

    id = Column(Integer, primary_key=True)
    video_id = Column(Integer, ForeignKey("videos.id"))
    mode = Column(String, nullable=False)  # news | blog
    title = Column(String)
    slug = Column(String)
    deck = Column(Text)
    article_body = Column(Text)
    primary_scripture = Column(String)
    seo_title = Column(String)
    meta_description = Column(Text)
    tags = Column(Text)           # JSON string
    seo_score = Column(Integer)
    risk_level = Column(String)   # LOW, MEDIUM, HIGH
    risk_status = Column(String)  # PASS, REVIEW, FAIL
    reviewer_notes = Column(Text) # JSON string
    status = Column(String, default="draft")  # draft, published
    html_path = Column(String)
    total_cost = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)
    published_at = Column(DateTime)

    video = relationship("Video", back_populates="articles")
    costs = relationship("ArticleCost", back_populates="article", cascade="all, delete-orphan")


class ArticleCost(Base):
    __tablename__ = "article_costs"

    id = Column(Integer, primary_key=True)
    article_id = Column(Integer, ForeignKey("articles.id"))
    model_name = Column(String)
    input_tokens = Column(Integer)
    output_tokens = Column(Integer)
    estimated_cost = Column(Float)

    article = relationship("Article", back_populates="costs")


class FailedTranscript(Base):
    __tablename__ = "failed_transcripts"

    id = Column(Integer, primary_key=True)
    youtube_video_id = Column(String, unique=True)
    reason = Column(String)
    last_attempted_at = Column(DateTime, default=datetime.utcnow)
    retry_after = Column(DateTime)
