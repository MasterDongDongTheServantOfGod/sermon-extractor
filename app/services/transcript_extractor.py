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
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import httpx
from dotenv import load_dotenv

load_dotenv()

SUPADATA_API_KEY = os.getenv("SUPADATA_API_KEY", "")
TRANSCRIPT_TIMEOUT = 8  # seconds per provider attempt
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


def _log_provider_success(provider: str, video_id: str) -> None:
    print(f"[Transcript] provider succeeded: {provider} video_id={video_id}", flush=True)


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
        with httpx.Client(timeout=TRANSCRIPT_TIMEOUT) as client:
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
            _record_failure(video_id, FAILURE_TRANSCRIPT_NOT_AVAILABLE)
    except Exception as exc:
        reason = _classify_error(exc)
        _record_failure(video_id, reason)
        if reason == FAILURE_TRANSCRIPT_TIMEOUT:
            print(f"[Transcript] timeout: video_id={video_id} provider=supadata", flush=True)
        else:
            print(
                f"[Transcript] extraction failed: video_id={video_id} "
                f"provider=supadata error={type(exc).__name__}",
                flush=True,
            )
    return None


def _extract_single(video_id: str) -> Optional[Tuple[str, str]]:
    """
    Try transcript providers in sequence for one video.
    Returns (text, type) or None within TRANSCRIPT_TIMEOUT seconds per attempt.
    """
    methods = []
    if SUPADATA_API_KEY:
        methods.append(("supadata", _extract_via_supadata))
    methods.extend([
        ("youtube_transcript_api", _extract_via_transcript_api),
        ("yt_dlp", _extract_via_ytdlp),
    ])

    for provider, method in methods:
        result = _run_provider_with_timeout(provider, method, video_id)
        if result:
            _log_provider_success(provider, video_id)
            return result

    _record_failure(video_id, FAILURE_TRANSCRIPT_NOT_AVAILABLE)
    print(f"[Transcript] not available: video_id={video_id}", flush=True)
    return None


def _run_provider_with_timeout(
    provider: str,
    method,
    video_id: str,
) -> Optional[Tuple[str, str]]:
    result_holder: List[Optional[Tuple[str, str]]] = [None]
    exc_holder: List[Optional[Exception]] = [None]

    def run():
        try:
            result_holder[0] = method(video_id)
        except Exception as exc:
            exc_holder[0] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=TRANSCRIPT_TIMEOUT)

    if t.is_alive():
        _record_failure(video_id, FAILURE_TRANSCRIPT_TIMEOUT)
        print(f"[Transcript] timeout: video_id={video_id} provider={provider}", flush=True)
        return None

    if exc_holder[0]:
        reason = _classify_error(exc_holder[0])
        _record_failure(video_id, reason)
        if reason == FAILURE_BOT_VERIFICATION:
            print(f"[Transcript] yt-dlp bot verification blocked: video_id={video_id}", flush=True)
        elif reason == FAILURE_TRANSCRIPT_TIMEOUT:
            print(f"[Transcript] timeout: video_id={video_id} provider={provider}", flush=True)
        else:
            print(
                f"[Transcript] extraction failed: video_id={video_id} "
                f"provider={provider} error={type(exc_holder[0]).__name__}",
                flush=True,
            )
        return None

    if not result_holder[0]:
        _record_failure(video_id, FAILURE_TRANSCRIPT_NOT_AVAILABLE)
    return result_holder[0]


def _extract_provider_parallel(
    video_ids: List[str],
    provider: str,
    method,
    max_concurrent: int,
) -> Dict[str, Tuple[str, str]]:
    executor = ThreadPoolExecutor(max_workers=max(1, max_concurrent))
    pending = {}
    remaining = iter(video_ids)
    shutdown_started = False

    def submit_next() -> bool:
        try:
            vid_id = next(remaining)
        except StopIteration:
            return False
        pending[executor.submit(_run_provider_with_timeout, provider, method, vid_id)] = vid_id
        return True

    try:
        for _ in range(min(max(1, max_concurrent), len(video_ids))):
            submit_next()

        while pending:
            done, _ = wait(
                pending.keys(),
                timeout=TRANSCRIPT_TIMEOUT + 1,
                return_when=FIRST_COMPLETED,
            )
            if not done:
                continue

            success: Optional[Tuple[str, Tuple[str, str]]] = None
            for future in done:
                vid_id = pending.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    _record_failure(vid_id, _classify_error(exc))
                    result = None

                if result and not success:
                    success = (vid_id, result)

            if success:
                vid_id, result = success
                _log_provider_success(provider, vid_id)
                for other in pending:
                    other.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                shutdown_started = True
                return {vid_id: result}

            if not pending:
                for _ in range(min(max(1, max_concurrent), len(video_ids))):
                    submit_next()

        return {}
    finally:
        if not shutdown_started:
            executor.shutdown(wait=True, cancel_futures=True)


def extract_transcripts_parallel(
    video_ids: List[str],
    max_concurrent: int = 5,
    supadata_limit: Optional[int] = None,
) -> Dict[str, Tuple[str, str]]:
    """
    Extract transcripts for scored videos. Returns after the first provider success.
    """
    video_ids = list(video_ids)
    results: Dict[str, Tuple[str, str]] = {}
    with _failure_lock:
        for vid_id in video_ids:
            _last_failure_reasons.pop(vid_id, None)

    print(f"[Transcript] candidate count: {len(video_ids)}", flush=True)
    print(f"[Transcript] parallel limit: {max_concurrent}", flush=True)
    print(f"[Transcript] timeout seconds: {TRANSCRIPT_TIMEOUT}", flush=True)
    print(f"[Transcript] Supadata enabled: {_bool_text(SUPADATA_API_KEY)}", flush=True)

    if SUPADATA_API_KEY:
        supadata_ids = video_ids[:supadata_limit] if supadata_limit else video_ids
        results = _extract_provider_parallel(
            supadata_ids,
            "supadata",
            _extract_via_supadata,
            max_concurrent,
        )
        if results:
            return results

    for provider, method in [
        ("youtube_transcript_api", _extract_via_transcript_api),
        ("yt_dlp", _extract_via_ytdlp),
    ]:
        results = _extract_provider_parallel(video_ids, provider, method, max_concurrent)
        if results:
            return results

    return results


def extract_supadata_batch(video_ids: List[str]) -> Dict[str, Tuple[str, str]]:
    """Try Supadata for a list of video IDs. Returns successes."""
    results: Dict[str, Tuple[str, str]] = {}
    for vid_id in video_ids:
        result = _extract_via_supadata(vid_id)
        if result:
            _log_provider_success("supadata", vid_id)
            results[vid_id] = result
    return results
