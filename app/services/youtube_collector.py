import os
import re
from datetime import datetime, timezone
from typing import List, Dict, Optional

from dotenv import load_dotenv

load_dotenv()

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")


def _build_client():
    from googleapiclient.discovery import build
    return build("youtube", "v3", developerKey=YOUTUBE_API_KEY)


def _parse_duration(duration: str) -> int:
    """Parse ISO 8601 duration (PT1H30M45S) to seconds."""
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration)
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def _resolve_channel_id(youtube, raw_id: str) -> Optional[str]:
    """Resolve @handle or channel name to UC... channel ID."""
    if raw_id.startswith("UC"):
        return raw_id

    # Try as a handle (@username)
    handle = raw_id.lstrip("@")
    try:
        response = youtube.channels().list(
            part="id",
            forHandle=handle
        ).execute()
        items = response.get("items", [])
        if items:
            return items[0]["id"]
    except Exception:
        pass

    # Try as a custom URL / username
    try:
        response = youtube.channels().list(
            part="id",
            forUsername=handle
        ).execute()
        items = response.get("items", [])
        if items:
            return items[0]["id"]
    except Exception:
        pass

    return None


def _uploads_playlist_id(channel_id: str) -> str:
    """Convert UC... channel ID to UU... uploads playlist ID."""
    if channel_id.startswith("UC"):
        return "UU" + channel_id[2:]
    return channel_id


def collect_recent_videos(raw_channel_id: str, max_results: int = 30) -> List[Dict]:
    """
    Collect recent videos from a YouTube channel.
    Returns list of video dicts with metadata.
    """
    if not YOUTUBE_API_KEY:
        raise RuntimeError("YOUTUBE_API_KEY not set")

    youtube = _build_client()

    channel_id = _resolve_channel_id(youtube, raw_channel_id)
    if not channel_id:
        raise ValueError(f"Could not resolve channel ID for: {raw_channel_id}")

    uploads_id = _uploads_playlist_id(channel_id)

    # Collect video IDs from uploads playlist
    video_ids = []
    next_page_token = None

    while len(video_ids) < max_results:
        batch_size = min(50, max_results - len(video_ids))
        response = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=uploads_id,
            maxResults=batch_size,
            pageToken=next_page_token
        ).execute()

        for item in response.get("items", []):
            video_ids.append(item["contentDetails"]["videoId"])

        next_page_token = response.get("nextPageToken")
        if not next_page_token:
            break

    if not video_ids:
        return []

    # Enrich with video details (batch up to 50)
    videos_response = youtube.videos().list(
        part="snippet,contentDetails,statistics",
        id=",".join(video_ids[:50])
    ).execute()

    results = []
    for item in videos_response.get("items", []):
        snippet = item["snippet"]
        content_details = item["contentDetails"]
        statistics = item.get("statistics", {})

        published_str = snippet.get("publishedAt", "")
        try:
            published_at = datetime.fromisoformat(published_str.replace("Z", "+00:00"))
        except ValueError:
            published_at = datetime.now(timezone.utc)

        duration_seconds = _parse_duration(content_details.get("duration", "PT0S"))
        has_captions = content_details.get("caption", "false") == "true"

        thumbnails = snippet.get("thumbnails", {})
        thumbnail_url = (
            thumbnails.get("maxres", {}).get("url")
            or thumbnails.get("high", {}).get("url")
            or thumbnails.get("medium", {}).get("url")
            or ""
        )

        results.append({
            "youtube_video_id": item["id"],
            "title": snippet.get("title", ""),
            "description": snippet.get("description", ""),
            "published_at": published_at,
            "duration_seconds": duration_seconds,
            "view_count": int(statistics.get("viewCount", 0)),
            "comment_count": int(statistics.get("commentCount", 0)),
            "thumbnail_url": thumbnail_url,
            "has_captions": has_captions,
        })

    return results
