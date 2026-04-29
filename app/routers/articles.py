import json
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import Article, ArticleCost, Channel, FailedTranscript, Video
from app.services import (
    gemini_summarizer,
    openai_writer,
    risk_reviewer,
    scorer,
    seo_scorer,
    static_publisher,
    transcript_extractor,
    youtube_collector,
)

router = APIRouter()

CANDIDATE_LIMIT = 20
COLLECT_MAX_RESULTS = int(os.getenv("YOUTUBE_COLLECT_MAX_RESULTS", "50"))
PARALLEL_LIMIT = 5
SUPADATA_FALLBACK_COUNT = 3
TRANSCRIPT_SCAN_LIMIT = int(os.getenv("TRANSCRIPT_SCAN_LIMIT", "40"))


class GenerateRequest(BaseModel):
    mode: str = "news"   # news | blog
    word_count: int = 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _serialize_article(a: Article) -> dict:
    tags = json.loads(a.tags) if a.tags else []
    reviewer_notes = json.loads(a.reviewer_notes) if a.reviewer_notes else []
    video = a.video
    channel = video.channel if video else None
    return {
        "id": a.id,
        "title": a.title,
        "deck": a.deck,
        "article_body": a.article_body,
        "primary_scripture": a.primary_scripture,
        "seo_title": a.seo_title,
        "meta_description": a.meta_description,
        "tags": tags,
        "mode": a.mode,
        "status": a.status,
        "seo_score": a.seo_score,
        "risk_level": a.risk_level,
        "risk_status": a.risk_status,
        "reviewer_notes": reviewer_notes,
        "total_cost": a.total_cost,
        "html_path": a.html_path,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "published_at": a.published_at.isoformat() if a.published_at else None,
        "costs": [
            {
                "model_name": c.model_name,
                "input_tokens": c.input_tokens,
                "output_tokens": c.output_tokens,
                "estimated_cost": c.estimated_cost,
            }
            for c in (a.costs or [])
        ],
        "video": {
            "youtube_video_id": video.youtube_video_id,
            "title": video.title,
            "thumbnail_url": video.thumbnail_url,
            "pastor_name": channel.pastor_name if channel else "Unknown",
            "church_name": channel.channel_title if channel else "",
            "score": video.score,
            "transcript_type": video.transcript_type,
            "published_at": video.published_at.isoformat() if video.published_at else None,
        } if video else None,
    }


def _save_cost(db: Session, article_id: int, model_name: str, result: dict) -> None:
    if result.get("input_tokens"):
        db.add(ArticleCost(
            article_id=article_id,
            model_name=model_name,
            input_tokens=result.get("input_tokens", 0),
            output_tokens=result.get("output_tokens", 0),
            estimated_cost=result.get("cost", 0.0),
        ))


def _pipeline_error(message: str, hint: str, **diagnostics):
    return HTTPException(
        status_code=422,
        detail={
            "message": message,
            "hint": hint,
            "diagnostics": diagnostics,
        },
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/generate", include_in_schema=False)
def generate_get_hint(request: Request):
    accept = request.headers.get("accept", "")
    if "application/json" not in accept:
        return RedirectResponse(url="/#generate", status_code=303)
    raise HTTPException(
        status_code=405,
        detail={
            "message": "Article generation requires a POST request.",
            "hint": "Use POST /api/articles/generate with JSON body {mode, word_count}.",
        },
        headers={"Allow": "POST"},
    )


@router.post("/generate")
def generate_article(data: GenerateRequest, db: Session = Depends(get_db)):
    """Full pipeline: collect → score → transcript → summarize → write → SEO → risk → save."""

    # 1. Active channels
    channels = db.query(Channel).filter(Channel.is_active == True).all()
    if not channels:
        raise HTTPException(400, "No active channels. Add pastor channels first.")

    # 2. Collect recent videos from all channels
    all_candidates = []
    collection_errors = []
    for ch in channels:
        try:
            raw_videos = youtube_collector.collect_recent_videos(
                ch.channel_id,
                max_results=COLLECT_MAX_RESULTS,
            )
            for v in raw_videos:
                v["pastor_name"] = ch.pastor_name
                v["church_name"] = ch.channel_title or ch.pastor_name
                v["channel_db_id"] = ch.id
            all_candidates.extend(raw_videos)
        except Exception as exc:
            collection_errors.append({
                "channel_id": ch.channel_id,
                "pastor_name": ch.pastor_name,
                "error": str(exc),
            })
            continue  # skip failing channels, keep going

    if not all_candidates:
        raise HTTPException(
            400,
            {
                "message": "Could not retrieve videos from any registered channel.",
                "hint": "Check that active channels are valid and YOUTUBE_API_KEY is configured.",
                "diagnostics": {"collection_errors": collection_errors},
            },
        )

    # 3. Already-published video IDs + failed transcript IDs
    used_video_ids = {
        v.youtube_video_id
        for v in db.query(Video).filter(Video.articles.any()).all()
    }
    failed_ids = {
        f.youtube_video_id for f in db.query(FailedTranscript).all()
    }

    # 4. Filter candidates
    filtered = []
    filter_counts = {
        "collected": len(all_candidates),
        "already_used": 0,
        "previous_transcript_failures": 0,
        "hard_filter_failures": 0,
        "usable_candidates": 0,
    }
    hard_filter_reasons = {}
    for v in all_candidates:
        vid_id = v["youtube_video_id"]
        if vid_id in used_video_ids:
            filter_counts["already_used"] += 1
            continue
        if vid_id in failed_ids:
            filter_counts["previous_transcript_failures"] += 1
            continue
        ok, reason = scorer.passes_hard_filters(v)
        if not ok:
            filter_counts["hard_filter_failures"] += 1
            hard_filter_reasons[reason] = hard_filter_reasons.get(reason, 0) + 1
            continue
        is_new = not db.query(Video).filter(Video.youtube_video_id == vid_id).first()
        v["is_new"] = is_new
        v["computed_score"] = scorer.score_video(v, is_new=is_new)
        filtered.append(v)
    filter_counts["usable_candidates"] = len(filtered)

    if not filtered:
        raise _pipeline_error(
            "No suitable video candidates after filtering.",
            "Add another active channel, wait for new uploads, or increase YOUTUBE_COLLECT_MAX_RESULTS.",
            filter_counts=filter_counts,
            hard_filter_reasons=hard_filter_reasons,
            collection_errors=collection_errors,
        )

    # 5. Sort by score and scan enough candidates to survive missing captions.
    filtered.sort(key=lambda x: x["computed_score"], reverse=True)
    transcript_candidates = filtered[:TRANSCRIPT_SCAN_LIMIT]
    selected_video = None
    selected_transcript = None
    selected_type = None
    transcript_attempted = []

    # 6-8. Try cheap transcript extraction in scored batches; use Supadata per batch.
    for start in range(0, len(transcript_candidates), CANDIDATE_LIMIT):
        batch = transcript_candidates[start:start + CANDIDATE_LIMIT]
        batch_ids = [v["youtube_video_id"] for v in batch]
        transcript_attempted.extend(batch_ids)

        transcripts = transcript_extractor.extract_transcripts_parallel(
            batch_ids,
            max_concurrent=PARALLEL_LIMIT,
        )

        for candidate in batch:
            if candidate["youtube_video_id"] in transcripts:
                selected_video = candidate
                selected_transcript, selected_type = transcripts[candidate["youtube_video_id"]]
                break

        if selected_video:
            break

        fallback_ids = batch_ids[:SUPADATA_FALLBACK_COUNT]
        fallback_results = transcript_extractor.extract_supadata_batch(fallback_ids)
        for candidate in batch[:SUPADATA_FALLBACK_COUNT]:
            if candidate["youtube_video_id"] in fallback_results:
                selected_video = candidate
                selected_transcript, selected_type = fallback_results[candidate["youtube_video_id"]]
                break

        if selected_video:
            break

    # 9. No transcript found → record failures and bail
    if not selected_video or not selected_transcript:
        for candidate in transcript_candidates[:5]:
            vid_id = candidate["youtube_video_id"]
            existing = db.query(FailedTranscript).filter(FailedTranscript.youtube_video_id == vid_id).first()
            if not existing:
                db.add(FailedTranscript(
                    youtube_video_id=vid_id,
                    reason="No usable transcript found from any method",
                ))
        db.commit()
        raise _pipeline_error(
            "No usable transcript found for any candidate video.",
            "Try again later, add channels with English captions, or configure SUPADATA_API_KEY for fallback transcripts.",
            filter_counts=filter_counts,
            transcript_candidates=len(transcript_candidates),
            transcript_attempted=transcript_attempted,
            supadata_enabled=bool(transcript_extractor.SUPADATA_API_KEY),
        )

    # 10. Save / update video record
    video_record = db.query(Video).filter(
        Video.youtube_video_id == selected_video["youtube_video_id"]
    ).first()

    if not video_record:
        video_record = Video(
            youtube_video_id=selected_video["youtube_video_id"],
            channel_id=selected_video["channel_db_id"],
            title=selected_video["title"],
            published_at=selected_video.get("published_at"),
            duration_seconds=selected_video.get("duration_seconds"),
            view_count=selected_video.get("view_count"),
            comment_count=selected_video.get("comment_count", 0),
            thumbnail_url=selected_video.get("thumbnail_url"),
            transcript_status="available",
            transcript_type=selected_type,
            score=selected_video["computed_score"],
        )
        db.add(video_record)
    else:
        video_record.transcript_status = "available"
        video_record.transcript_type = selected_type
        video_record.score = selected_video["computed_score"]
    db.commit()
    db.refresh(video_record)

    # 11. Gemini summarization
    gemini_result = gemini_summarizer.summarize_sermon(
        transcript=selected_transcript,
        title=selected_video["title"],
        pastor_name=selected_video["pastor_name"],
    )

    # 12. GPT article generation
    published_date = (
        selected_video["published_at"].strftime("%B %d, %Y")
        if selected_video.get("published_at")
        else ""
    )
    video_url = f"https://www.youtube.com/watch?v={selected_video['youtube_video_id']}"

    gpt_result = openai_writer.generate_article(
        mode=data.mode,
        pastor_name=selected_video["pastor_name"],
        church_or_ministry=selected_video.get("church_name", ""),
        sermon_title=selected_video["title"],
        video_url=video_url,
        published_date=published_date,
        transcript_quality=selected_type or "auto",
        primary_scripture=gemini_result.get("primary_scripture", ""),
        strong_quotes=gemini_result.get("strong_quotes", []),
        summary=gemini_result.get("summary", ""),
        keywords=gemini_result.get("keywords", []),
        main_theme=gemini_result.get("main_theme", ""),
        word_count=data.word_count,
    )

    # 13. SEO score
    seo_result = seo_scorer.score_seo(
        title=gpt_result.get("title", ""),
        article_body=gpt_result.get("article_body", ""),
        meta_description=gpt_result.get("meta_description", ""),
        keywords=gemini_result.get("keywords", []),
        sources=[video_url],
        primary_scripture=gemini_result.get("primary_scripture", ""),
    )

    # 14. Risk review
    risk_result = risk_reviewer.review_article(
        transcript_summary=gemini_result.get("summary", ""),
        strong_quotes=gemini_result.get("strong_quotes", []),
        article=gpt_result.get("article_body", ""),
        primary_scripture=gemini_result.get("primary_scripture", ""),
        pastor_name=selected_video["pastor_name"],
    )

    # 15. Totals
    total_cost = (
        gemini_result.get("cost", 0.0)
        + gpt_result.get("cost", 0.0)
        + risk_result.get("cost", 0.0)
    )

    # 16. Save article
    article = Article(
        video_id=video_record.id,
        mode=data.mode,
        title=gpt_result.get("title", selected_video["title"]),
        deck=gpt_result.get("deck", ""),
        article_body=gpt_result.get("article_body", ""),
        primary_scripture=gemini_result.get("primary_scripture", ""),
        seo_title=gpt_result.get("seo_title", ""),
        meta_description=gpt_result.get("meta_description", ""),
        tags=json.dumps(gpt_result.get("tags", [])),
        seo_score=seo_result["seo_score"],
        risk_level=risk_result.get("risk_level", "MEDIUM"),
        risk_status=risk_result.get("status", "REVIEW"),
        reviewer_notes=json.dumps(risk_result.get("reviewer_notes", [])),
        status="draft",
        total_cost=total_cost,
    )
    db.add(article)
    db.commit()
    db.refresh(article)

    _save_cost(db, article.id, "gemini_summarizer", gemini_result)
    _save_cost(db, article.id, "openai_writer", gpt_result)
    _save_cost(db, article.id, "openai_risk_reviewer", risk_result)
    db.commit()

    # Reload with relationships for response
    article = (
        db.query(Article)
        .options(joinedload(Article.video).joinedload(Video.channel), joinedload(Article.costs))
        .filter(Article.id == article.id)
        .first()
    )

    return {
        "article_id": article.id,
        **_serialize_article(article),
        "scores": {
            "seo_score": seo_result["seo_score"],
            "seo_checks": seo_result["checks"],
            "risk_level": risk_result.get("risk_level"),
            "risk_status": risk_result.get("status"),
            "reviewer_notes": risk_result.get("reviewer_notes", []),
            "quote_accuracy": risk_result.get("quote_accuracy"),
            "scripture_accuracy": risk_result.get("scripture_accuracy"),
        },
        "cost_breakdown": {
            "gemini": round(gemini_result.get("cost", 0.0), 6),
            "openai_writer": round(gpt_result.get("cost", 0.0), 6),
            "openai_risk": round(risk_result.get("cost", 0.0), 6),
            "total": round(total_cost, 6),
        },
    }


@router.get("")
def list_articles(db: Session = Depends(get_db)):
    articles = (
        db.query(Article)
        .options(joinedload(Article.video).joinedload(Video.channel))
        .order_by(Article.created_at.desc())
        .all()
    )
    result = []
    for a in articles:
        tags = json.loads(a.tags) if a.tags else []
        video = a.video
        channel = video.channel if video else None
        result.append({
            "id": a.id,
            "title": a.title,
            "mode": a.mode,
            "status": a.status,
            "primary_scripture": a.primary_scripture,
            "seo_score": a.seo_score,
            "risk_level": a.risk_level,
            "risk_status": a.risk_status,
            "total_cost": a.total_cost,
            "tags": tags,
            "html_path": a.html_path,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "published_at": a.published_at.isoformat() if a.published_at else None,
            "video": {
                "youtube_video_id": video.youtube_video_id,
                "title": video.title,
                "thumbnail_url": video.thumbnail_url,
                "pastor_name": channel.pastor_name if channel else "Unknown",
            } if video else None,
        })
    return result


@router.get("/{article_id}")
def get_article(article_id: int, db: Session = Depends(get_db)):
    article = (
        db.query(Article)
        .options(
            joinedload(Article.video).joinedload(Video.channel),
            joinedload(Article.costs),
        )
        .filter(Article.id == article_id)
        .first()
    )
    if not article:
        raise HTTPException(404, "Article not found")
    return _serialize_article(article)


@router.post("/{article_id}/publish")
def publish_article_route(article_id: int, db: Session = Depends(get_db)):
    article = (
        db.query(Article)
        .options(joinedload(Article.video).joinedload(Video.channel))
        .filter(Article.id == article_id)
        .first()
    )
    if not article:
        raise HTTPException(404, "Article not found")
    if article.status == "published":
        raise HTTPException(400, "Article already published")

    tags = json.loads(article.tags) if article.tags else []
    video = article.video
    channel = video.channel if video else None

    result = static_publisher.publish_article(
        article_id=article.id,
        title=article.title or "",
        deck=article.deck or "",
        article_body=article.article_body or "",
        primary_scripture=article.primary_scripture or "",
        seo_title=article.seo_title or article.title or "",
        meta_description=article.meta_description or "",
        tags=tags,
        pastor_name=channel.pastor_name if channel else "Unknown",
        church_name=channel.channel_title if channel else "",
        sermon_title=video.title if video else "",
        video_url=f"https://www.youtube.com/watch?v={video.youtube_video_id}" if video else "",
        thumbnail_url=video.thumbnail_url if video else "",
        published_date=(
            video.published_at.strftime("%B %d, %Y")
            if (video and video.published_at)
            else ""
        ),
        seo_score=article.seo_score or 0,
        risk_level=article.risk_level or "LOW",
    )

    article.status = "published"
    article.html_path = result["html_path"]
    article.published_at = datetime.utcnow()
    db.commit()

    return {"success": True, "html_path": result["html_path"], "slug": result["slug"]}


@router.delete("/{article_id}")
def delete_article(article_id: int, db: Session = Depends(get_db)):
    article = db.query(Article).filter(Article.id == article_id).first()
    if not article:
        raise HTTPException(404, "Article not found")
    db.query(ArticleCost).filter(ArticleCost.article_id == article_id).delete()
    db.delete(article)
    db.commit()
    return {"success": True}
