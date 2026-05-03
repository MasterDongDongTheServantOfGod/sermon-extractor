import base64
import json
import glob
import logging
import os
import stat
import tempfile
import threading
from pathlib import Path
from typing import Optional, Tuple, Dict, List
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed

import httpx
from dotenv import load_dotenv

load_dotenv()

SUPADATA_API_KEY = os.getenv("SUPADATA_API_KEY", "")
TRANSCRIPT_TIMEOUT = 20  # seconds per extraction attempt
_YT_COOKIES_PATH = "/tmp/youtube-cookies.txt"
YT_COOKIES_PATH = _YT_COOKIES_PATH

FAILURE_BOT_VERIFICATION = "YouTube bot verification blocked yt-dlp access"
FAILURE_TRANSCRIPT_NOT_AVAILABLE = "Transcript not available"
FAILURE_TRANSCRIPT_TIMEOUT = "Transcript extraction timeout"

logger = logging.getLogger(__name__)

_cookies_lock = threading.Lock()
_cookies_initialized = False
_cookies_path_cache: Optional[str] = None

_failure_lock = threading.Lock()
_last_failure_reasons: Dict[str, str] = {}
_FAILURE_PRIORITY = {
    FAILURE_TRANSCRIPT_NOT_AVAILABLE: 1,
    FAILURE_TRANSCRIPT_TIMEOUT: 2,
    FAILURE_BOT_VERIFICATION: 3,
}


def _bool_text(value: object) -> str:
    return "true" if value else "false"


def get_youtube_cookies_path() -> Optional[str]:
    """
    Decode Render-provided YouTube cookies into a temporary cookies.txt file.

    Returns the temporary path when YT_COOKIES_BASE64 is configured and valid,
    otherwise returns None. Cookie contents are never logged.
    """
    global _cookies_initialized, _cookies_path_cache

    with _cookies_lock:
        if _cookies_initialized:
            return _cookies_path_cache

        encoded = os.getenv("YT_COOKIES_BASE64")
        if not encoded:
            _cookies_initialized = True
            _cookies_path_cache = None
            print("[YT Cookies] enabled: false", flush=True)
            return None

        try:
            encoded = encoded.strip().strip('"').strip("'")
            if "," in encoded and encoded.lower().startswith("data:"):
                encoded = encoded.split(",", 1)[1]
            normalized = "".join(encoded.split())
            cookies_bytes = base64.b64decode(normalized, validate=True)
            if not cookies_bytes.strip():
                raise ValueError("decoded cookies file is empty")

            cookies_path = Path(_YT_COOKIES_PATH)
            cookies_path.parent.mkdir(parents=True, exist_ok=True)
            cookies_path.write_bytes(cookies_bytes)
            try:
                os.chmod(cookies_path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass

            _cookies_path_cache = str(cookies_path)
            _cookies_initialized = True
            print("[YT Cookies] enabled: true", flush=True)
            print("[YT Cookies] file created: true", flush=True)
            return _cookies_path_cache
        except Exception as exc:
            _cookies_initialized = True
            _cookies_path_cache = None
            print("[YT Cookies] enabled: false", flush=True)
            print("[YT Cookies] file created: false", flush=True)
            print("[YT Cookies] error:", type(exc).__name__, flush=True)
            return None


def getYoutubeCookiesPath() -> Optional[str]:
    """Backward-compatible alias for existing callers."""
    return get_youtube_cookies_path()


def _is_bot_verification_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "not a bot" in message
        or "sign in to confirm" in message
        or "cookies-from-browser" in message
        or "use --cookies" in message
    )


def _classify_error(exc: Exception) -> str:
    if _is_bot_verification_error(exc):
        return FAILURE_BOT_VERIFICATION
    if "timed out" in str(exc).lower() or "timeout" in str(exc).lower():
        return FAILURE_TRANSCRIPT_TIMEOUT
    return FAILURE_TRANSCRIPT_NOT_AVAILABLE


def _record_failure(video_id: str, reason: str) -> None:
    with _failure_lock:
        existing = _last_failure_reasons.get(video_id)
        if not existing or _FAILURE_PRIORITY.get(reason, 0) >= _FAILURE_PRIORITY.get(existing, 0):
            _last_failure_reasons[video_id] = reason


def getTranscriptFailureReasons(video_ids: Optional[List[str]] = None) -> Dict[str, str]:
    """Return recent transcript failure reasons without exposing sensitive data."""
    with _failure_lock:
        if video_ids is None:
            return dict(_last_failure_reasons)
        return {
            video_id: _last_failure_reasons[video_id]
            for video_id in video_ids
            if video_id in _last_failure_reasons
        }


def get_transcript_failure_reasons(video_ids: Optional[List[str]] = None) -> Dict[str, str]:
    """Snake-case alias for Python callers."""
    return getTranscriptFailureReasons(video_ids)


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

    except Exception as exc:
        reason = _classify_error(exc)
        _record_failure(video_id, reason)
        if reason == FAILURE_BOT_VERIFICATION:
            logger.warning("%s for video_id=%s", reason, video_id)
        elif reason == FAILURE_TRANSCRIPT_TIMEOUT:
            print(f"[Transcript] timeout: video_id={video_id}", flush=True)
        else:
            print(f"[Transcript] not available: video_id={video_id}", flush=True)

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
                "noplaylist": True,
                "outtmpl": os.path.join(tmpdir, "%(id)s"),
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": TRANSCRIPT_TIMEOUT,
            }

            cookies_path = get_youtube_cookies_path()
            if cookies_path:
                ydl_opts["cookiefile"] = cookies_path
            print(f"[Transcript] yt-dlp cookies enabled: {_bool_text(cookies_path)}", flush=True)

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    retcode = ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
                    if retcode:
                        raise RuntimeError(f"yt-dlp exited with status {retcode}")
            except Exception as exc:
                reason = _classify_error(exc)
                _record_failure(video_id, reason)
                if reason == FAILURE_BOT_VERIFICATION:
                    print(f"[Transcript] yt-dlp bot verification blocked: video_id={video_id}", flush=True)
                elif reason == FAILURE_TRANSCRIPT_TIMEOUT:
                    print(f"[Transcript] timeout: video_id={video_id}", flush=True)
                else:
                    print(f"[Transcript] extraction failed: video_id={video_id} error={type(exc).__name__}", flush=True)
                return None

            sub_files = glob.glob(os.path.join(tmpdir, "*.json3"))
            if not sub_files:
                _record_failure(video_id, FAILURE_TRANSCRIPT_NOT_AVAILABLE)
                print(f"[Transcript] not available: video_id={video_id}", flush=True)
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
            if not text:
                _record_failure(video_id, FAILURE_TRANSCRIPT_NOT_AVAILABLE)
                print(f"[Transcript] not available: video_id={video_id}", flush=True)
                return None
            return (text, kind) if text else None

    except Exception as exc:
        reason = _classify_error(exc)
        _record_failure(video_id, reason)
        if reason == FAILURE_BOT_VERIFICATION:
            print(f"[Transcript] yt-dlp bot verification blocked: video_id={video_id}", flush=True)
        elif reason == FAILURE_TRANSCRIPT_TIMEOUT:
            print(f"[Transcript] timeout: video_id={video_id}", flush=True)
        else:
            print(f"[Transcript] extraction failed: video_id={video_id} error={type(exc).__name__}", flush=True)

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

        if t.is_alive():
            _record_failure(video_id, FAILURE_TRANSCRIPT_TIMEOUT)
            logger.warning(
                "%s for video_id=%s via %s",
                FAILURE_TRANSCRIPT_TIMEOUT,
                video_id,
                method.__name__,
            )
            continue

        if exc_holder[0]:
            reason = _classify_error(exc_holder[0])
            _record_failure(video_id, reason)
            if reason == FAILURE_BOT_VERIFICATION:
                logger.warning("%s for video_id=%s", reason, video_id)
            elif reason == FAILURE_TRANSCRIPT_TIMEOUT:
                print(f"[Transcript] timeout: video_id={video_id}", flush=True)
            else:
                print(
                    f"[Transcript] extraction failed: video_id={video_id} "
                    f"error={type(exc_holder[0]).__name__}",
                    flush=True,
                )

        if result_holder[0]:
            return result_holder[0]

    _record_failure(video_id, FAILURE_TRANSCRIPT_NOT_AVAILABLE)
    print(f"[Transcript] not available: video_id={video_id}", flush=True)
    return None


def extract_transcripts_parallel(
    video_ids: List[str], max_concurrent: int = 5
) -> Dict[str, Tuple[str, str]]:
    """
    Extract transcripts for multiple videos in parallel (5 at a time).
    Returns {video_id: (text, type)} for successes only.
    """
    results: Dict[str, Tuple[str, str]] = {}
    with _failure_lock:
        for vid_id in video_ids:
            _last_failure_reasons.pop(vid_id, None)

    with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
        future_to_id = {
            executor.submit(_extract_single, vid_id): vid_id
            for vid_id in video_ids
        }
        try:
            for future in as_completed(future_to_id, timeout=TRANSCRIPT_TIMEOUT * 4):
                vid_id = future_to_id[future]
                try:
                    result = future.result(timeout=5)
                    if result:
                        results[vid_id] = result
                except Exception as exc:
                    _record_failure(vid_id, _classify_error(exc))
        except FuturesTimeoutError:
            for future, vid_id in future_to_id.items():
                if not future.done():
                    _record_failure(vid_id, FAILURE_TRANSCRIPT_TIMEOUT)
                    logger.warning("%s for video_id=%s", FAILURE_TRANSCRIPT_TIMEOUT, vid_id)

    return results


def extract_supadata_batch(video_ids: List[str]) -> Dict[str, Tuple[str, str]]:
    """Try Supadata for a list of video IDs. Returns successes."""
    results: Dict[str, Tuple[str, str]] = {}
    for vid_id in video_ids:
        result = _extract_via_supadata(vid_id)
        if result:
            results[vid_id] = result
    return results
