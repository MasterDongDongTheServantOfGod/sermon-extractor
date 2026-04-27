from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import Video

router = APIRouter()


@router.get("")
def list_videos(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    videos = (
        db.query(Video)
        .options(joinedload(Video.channel))
        .order_by(Video.score.desc(), Video.published_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return [
        {
            "id": v.id,
            "youtube_video_id": v.youtube_video_id,
            "title": v.title,
            "pastor_name": v.channel.pastor_name if v.channel else "Unknown",
            "published_at": v.published_at.isoformat() if v.published_at else None,
            "duration_seconds": v.duration_seconds,
            "view_count": v.view_count,
            "thumbnail_url": v.thumbnail_url,
            "transcript_status": v.transcript_status,
            "transcript_type": v.transcript_type,
            "score": v.score,
            "failure_reason": v.failure_reason,
        }
        for v in videos
    ]
