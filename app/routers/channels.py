from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.models import Channel

router = APIRouter()


class ChannelCreate(BaseModel):
    pastor_name: str
    channel_id: str
    channel_title: Optional[str] = ""


class ChannelUpdate(BaseModel):
    pastor_name: Optional[str] = None
    channel_title: Optional[str] = None
    is_active: Optional[bool] = None


def _serialize(c: Channel) -> dict:
    return {
        "id": c.id,
        "pastor_name": c.pastor_name,
        "channel_id": c.channel_id,
        "channel_title": c.channel_title,
        "is_active": c.is_active,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@router.get("")
def list_channels(db: Session = Depends(get_db)):
    return [_serialize(c) for c in db.query(Channel).order_by(Channel.created_at.desc()).all()]


@router.post("")
def create_channel(data: ChannelCreate, db: Session = Depends(get_db)):
    existing = db.query(Channel).filter(Channel.channel_id == data.channel_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="Channel already registered")

    channel = Channel(
        pastor_name=data.pastor_name,
        channel_id=data.channel_id.strip(),
        channel_title=data.channel_title or data.pastor_name,
    )
    db.add(channel)
    db.commit()
    db.refresh(channel)
    return _serialize(channel)


@router.put("/{channel_id}")
def update_channel(channel_id: int, data: ChannelUpdate, db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    if data.pastor_name is not None:
        channel.pastor_name = data.pastor_name
    if data.channel_title is not None:
        channel.channel_title = data.channel_title
    if data.is_active is not None:
        channel.is_active = data.is_active

    db.commit()
    return _serialize(channel)


@router.delete("/{channel_id}")
def delete_channel(channel_id: int, db: Session = Depends(get_db)):
    channel = db.query(Channel).filter(Channel.id == channel_id).first()
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    db.delete(channel)
    db.commit()
    return {"success": True}
