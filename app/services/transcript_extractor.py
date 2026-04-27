import os
import json
import glob
import tempfile
import threading
from typing import Optional, Tuple, Dict, List
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
from dotenv import load_dotenv

load_dotenv()

SUPADATA_API_KEY = os.getenv("SUPADATA_API_KEY", "")
TRANSCRIPT_TIMEOUT = 20  # seconds per extraction attempt


def _extract_via_transcript_api(video_id: str) -> Optional[Tuple[str, str]]:
    """Try youtube-transcript-api. Returns (text, type) or None."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound

        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)

        # Prefer manually uploaded English transcript
        for lang_codes in [["en", "en-US", "en-GB"], None]:
            try:
                if lang_codes:
                    t = transcript_list.find_manually_created_transcript(lang_codes)
                    kind = "manual"
                else:
                    t = transcript_list.find_generated_transcript(["en", "en-US"])
                    kind = "auto"
                data = t.fetch()
                text = " ".join(seg["text"] for seg in data if seg.get("text"))
                if text.strip():
                    return text.strip(), kind
            except Exception:
                continue

        # Last resort: try generated
        try:
            t = transcript_list.find_generated_transcript(["en"])
            data = t.fetch()
            text = " ".join(seg["text"] for seg in data if seg.get("text"))
            if text.strip():
                return text.strip(), "auto"
        except Exception:
            pass

    except Exception:
        pass

    return None


def _extract_via_ytdlp(video_id: str) -> Optional[Tuple[str, str]]:
    """Try yt-dlp subtitle download. Returns (text, type) or None."""
    try:
        import yt_dlp

        with tempfile.TemporaryDirectory() as tmpdir:
            ydl_opts = {
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": ["en", "en-US"],
                "subtitlesformat": "json3",
                "skip_download": True,
                "outtmpl": f"{tmpdir}/%(id)s",
                "quiet": True,
                "no_warnings": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])

            sub_files = glob.glob(f"{tmpdir}/*.json3")
            if not sub_files:
                return None

            # Determine type from filename
            kind = "manual"
            for f in sub_files:
                if ".auto." in f or ".a." in f:
                    kind = "auto"
                    break

            with open(sub_files[0], "r", encoding="utf-8") as fh:
                data = json.load(fh)

            texts = []
            for event in data.get("events", []):
                for seg in event.get("segs", []):
                    t = seg.get("utf8", "").strip()
                    if t and t != "\n":
                        texts.append(t)

            text = " ".join(texts).strip()
            return (text, kind) if text else None

    except Exception:
        pass

    return None


def _extract_via_supadata(video_id: str) -> Optional[Tuple[str, str]]:
    """Supadata API fallback. Returns (text, 'fallback') or None."""
    if not SUPADATA_API_KEY:
        return None
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(
                "https://api.supadata.ai/v1/youtube/transcript",
                params={"videoId": video_id, "lang": "en"},
                headers={"x-api-key": SUPADATA_API_KEY},
            )
            if resp.status_code == 200:
                payload = resp.json()
                content = payload.get("content", [])
                if isinstance(content, list):
                    text = " ".join(item.get("text", "") for item in content).strip()
                else:
                    text = str(content).strip()
                return (text, "fallback") if text else None
    except Exception:
        pass
    return None


def _extract_single(video_id: str) -> Optional[Tuple[str, str]]:
    """
    Try cheap extraction methods in sequence.
    Returns (text, type) or None within TRANSCRIPT_TIMEOUT seconds per attempt.
    """
    for method in [_extract_via_transcript_api, _extract_via_ytdlp]:
        result_holder: List[Optional[Tuple[str, str]]] = [None]
        exc_holder: List[Optional[Exception]] = [None]

        def run(m=method):
            try:
                result_holder[0] = m(video_id)
            except Exception as e:
                exc_holder[0] = e

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=TRANSCRIPT_TIMEOUT)

        if result_holder[0]:
            return result_holder[0]

    return None


def extract_transcripts_parallel(
    video_ids: List[str], max_concurrent: int = 5
) -> Dict[str, Tuple[str, str]]:
    """
    Extract transcripts for multiple videos in parallel (5 at a time).
    Returns {video_id: (text, type)} for successes only.
    """
    results: Dict[str, Tuple[str, str]] = {}

    with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
        future_to_id = {
            executor.submit(_extract_single, vid_id): vid_id
            for vid_id in video_ids
        }
        for future in as_completed(future_to_id, timeout=TRANSCRIPT_TIMEOUT * 4):
            vid_id = future_to_id[future]
            try:
                result = future.result(timeout=5)
                if result:
                    results[vid_id] = result
            except Exception:
                pass

    return results


def extract_supadata_batch(video_ids: List[str]) -> Dict[str, Tuple[str, str]]:
    """Try Supadata for a list of video IDs. Returns successes."""
    results: Dict[str, Tuple[str, str]] = {}
    for vid_id in video_ids:
        result = _extract_via_supadata(vid_id)
        if result:
            results[vid_id] = result
    return results
