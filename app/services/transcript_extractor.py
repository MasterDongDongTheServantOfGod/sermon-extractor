import base64
import html as html_lib
import json
import os
import re
import stat
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Optional, Tuple, Dict, List
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import httpx
from dotenv import load_dotenv

load_dotenv()

SUPADATA_API_KEY = os.getenv("SUPADATA_API_KEY", "")
TRANSCRIPT_TIMEOUT = 8  # seconds per provider attempt
FAST_PROVIDER_TIMEOUT = 5
SUPADATA_TIMEOUT = 5
YTDLP_SOCKET_TIMEOUT = 15
FAST_CANDIDATE_LIMIT = 30
DEEP_CANDIDATE_START = 30
DEEP_CANDIDATE_LIMIT = 20
FAST_PARALLEL_LIMIT = 4
DEEP_PARALLEL_LIMIT = 2
DEEP_YTDLP_LIMIT = 5
DEEP_SUPADATA_LIMIT = 3
SUPADATA_COOLDOWN_SECONDS = 600
_YT_COOKIES_PATH = "/tmp/youtube-cookies.txt"
YT_COOKIES_PATH = _YT_COOKIES_PATH
ENGLISH_TRANSCRIPT_LANGS = ["en", "en-US", "en-GB", "a.en"]
SUBTITLE_FORMAT_PREFERENCE = ["json3", "srv3", "ttml", "vtt"]

FAILURE_BOT_VERIFICATION = "YouTube bot verification blocked yt-dlp access"
FAILURE_TRANSCRIPT_NOT_AVAILABLE = "Transcript not available"
FAILURE_TRANSCRIPT_TIMEOUT = "Transcript extraction timeout"

REASON_TRANSCRIPT_NOT_AVAILABLE = "transcript_not_available"
REASON_TRANSCRIPTS_DISABLED = "transcripts_disabled"
REASON_VIDEO_UNAVAILABLE = "video_unavailable"
REASON_YOUTUBE_TRANSCRIPT_API_TIMEOUT = "youtube_transcript_api_timeout"
REASON_YTDLP_TIMEOUT = "ytdlp_timeout"
REASON_YTDLP_NO_SUBTITLES = "ytdlp_no_subtitles"
REASON_SUPADATA_429 = "supadata_429"
REASON_SUPADATA_ERROR = "supadata_error"
REASON_PROVIDER_BLOCKED = "provider_blocked"

VIDEO_LEVEL_TRANSCRIPT_FAILURES = {
    REASON_TRANSCRIPT_NOT_AVAILABLE,
    REASON_TRANSCRIPTS_DISABLED,
    REASON_VIDEO_UNAVAILABLE,
    REASON_YTDLP_NO_SUBTITLES,
    FAILURE_TRANSCRIPT_NOT_AVAILABLE,
}

_cookies_lock = threading.Lock()
_cookies_initialized = False
_cookies_path_cache: Optional[str] = None
_cookies_invalid_format_logged = False

_failure_lock = threading.Lock()
_last_failure_reasons: Dict[str, str] = {}
_provider_attempt_counts: Dict[str, int] = {}
_provider_timeout_counts: Dict[str, int] = {}
_supadata_status_counts: Dict[str, int] = {}
_supadata_failure_reason_counts: Dict[str, int] = {}
_supadata_cooldown_until = 0.0
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


def _log_invalid_netscape_cookie_format() -> None:
    global _cookies_invalid_format_logged
    with _cookies_lock:
        if _cookies_invalid_format_logged:
            return
        _cookies_invalid_format_logged = True
    print("[YT Cookies] invalid Netscape format", flush=True)


def is_youtube_cookiefile_valid(log_invalid: bool = True) -> bool:
    cookies_path = get_youtube_cookies_path()
    if not cookies_path:
        return False

    try:
        with open(cookies_path, "r", encoding="utf-8", errors="replace") as fh:
            first_line = fh.readline(256)
    except OSError:
        if log_invalid:
            _log_invalid_netscape_cookie_format()
        return False

    is_valid = "Netscape HTTP Cookie File" in first_line
    if not is_valid and log_invalid:
        _log_invalid_netscape_cookie_format()
    return is_valid


def _get_ytdlp_cookiefile() -> Optional[str]:
    cookies_path = get_youtube_cookies_path()
    if not cookies_path:
        return None
    return cookies_path if is_youtube_cookiefile_valid() else None


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


def _increment_counter(counter: Dict[str, int], key: str) -> None:
    with _failure_lock:
        counter[key] = counter.get(key, 0) + 1


def _record_provider_attempt(provider: str) -> None:
    _increment_counter(_provider_attempt_counts, provider)


def _record_provider_timeout(provider: str) -> None:
    _increment_counter(_provider_timeout_counts, provider)


def _record_supadata_status(status_code: int) -> None:
    _increment_counter(_supadata_status_counts, str(status_code))


def _record_supadata_failure(reason: str) -> None:
    _increment_counter(_supadata_failure_reason_counts, reason)


def _activate_supadata_cooldown() -> None:
    global _supadata_cooldown_until
    _supadata_cooldown_until = time.time() + SUPADATA_COOLDOWN_SECONDS


def is_supadata_cooldown_active() -> bool:
    return time.time() < _supadata_cooldown_until


def resetTranscriptProviderDiagnostics() -> None:
    with _failure_lock:
        _provider_attempt_counts.clear()
        _provider_timeout_counts.clear()
        _supadata_status_counts.clear()
        _supadata_failure_reason_counts.clear()


def getTranscriptProviderDiagnostics() -> Dict[str, Dict[str, int]]:
    with _failure_lock:
        return {
            "supadata_status_counts": dict(_supadata_status_counts),
            "supadata_failure_reason_counts": dict(_supadata_failure_reason_counts),
            "provider_attempt_counts": dict(_provider_attempt_counts),
            "provider_timeout_counts": dict(_provider_timeout_counts),
        }


def get_transcript_provider_diagnostics() -> Dict[str, Dict[str, int]]:
    return getTranscriptProviderDiagnostics()


def is_video_level_transcript_failure(reason: Optional[str]) -> bool:
    return reason in VIDEO_LEVEL_TRANSCRIPT_FAILURES


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


def _candidate_video_id(candidate) -> str:
    if isinstance(candidate, dict):
        return str(candidate.get("youtube_video_id", ""))
    return str(candidate)


def _new_mode_stats(mode: str, candidates: List) -> dict:
    return {
        "mode": mode,
        "candidate_count": len(candidates),
        "checked_candidates": 0,
        "success": 0,
        "failures": {},
        "video_failures": {},
        "other_language_captions_found": 0,
    }


def _increment_stat(stats: dict, key: str, amount: int = 1) -> None:
    stats[key] = stats.get(key, 0) + amount


def _record_mode_failure(stats: dict, video_id: str, reason: str) -> None:
    failures = stats.setdefault("failures", {})
    failures[reason] = failures.get(reason, 0) + 1
    _increment_stat(stats, reason)
    if is_video_level_transcript_failure(reason):
        stats.setdefault("video_failures", {})[video_id] = reason


def _segments_to_text(data) -> str:
    parts = []
    for segment in data:
        if isinstance(segment, dict):
            text = segment.get("text", "")
        else:
            text = getattr(segment, "text", "")
        text = str(text).replace("\n", " ").strip()
        if text:
            parts.append(text)
    return " ".join(parts).strip()


def _transcript_result(
    video_id: str,
    provider: str,
    transcript: str,
    language: str = "en",
    translated: bool = False,
    source_language: Optional[str] = None,
    target_language: Optional[str] = None,
    transcript_type: str = "auto",
) -> dict:
    return {
        "ok": True,
        "video_id": video_id,
        "provider": provider,
        "transcript": transcript,
        "language": language,
        "translated": translated,
        "source_language": source_language or language,
        "target_language": target_language,
        "transcript_type": transcript_type,
    }


def _provider_failure(
    video_id: str,
    provider: str,
    reason: str,
    other_language_captions_found: bool = False,
) -> dict:
    return {
        "ok": False,
        "video_id": video_id,
        "provider": provider,
        "reason": reason,
        "other_language_captions_found": other_language_captions_found,
    }


def _youtube_transcript_api_instance():
    from youtube_transcript_api import YouTubeTranscriptApi

    return YouTubeTranscriptApi()


def _list_youtube_transcripts(video_id: str):
    api = _youtube_transcript_api_instance()
    if hasattr(api, "list"):
        return api.list(video_id)

    # Backward compatibility for older youtube-transcript-api versions.
    from youtube_transcript_api import YouTubeTranscriptApi

    return YouTubeTranscriptApi.list_transcripts(video_id)


def _fetch_transcript_candidate(
    video_id: str,
    transcript,
    provider: str,
    translated: bool = False,
    source_language: Optional[str] = None,
) -> Optional[dict]:
    data = transcript.fetch()
    text = _segments_to_text(data)
    if not text:
        return None

    language = getattr(data, "language_code", None) or getattr(transcript, "language_code", "en")
    transcript_type = "auto" if getattr(transcript, "is_generated", False) else "manual"
    if translated:
        transcript_type = "auto"
    return _transcript_result(
        video_id=video_id,
        provider=provider,
        transcript=text,
        language=language,
        translated=translated,
        source_language=source_language or language,
        target_language="en" if translated else None,
        transcript_type=transcript_type,
    )


def _extract_youtube_transcript_api_result(video_id: str) -> dict:
    provider = "youtube_transcript_api"
    try:
        from youtube_transcript_api._errors import (
            InvalidVideoId,
            IpBlocked,
            NoTranscriptFound,
            PoTokenRequired,
            RequestBlocked,
            TranscriptsDisabled,
            VideoUnavailable,
        )

        transcript_list = _list_youtube_transcripts(video_id)

        for finder_name in ["find_manually_created_transcript", "find_generated_transcript"]:
            try:
                transcript = getattr(transcript_list, finder_name)(ENGLISH_TRANSCRIPT_LANGS)
                result = _fetch_transcript_candidate(video_id, transcript, provider)
                if result:
                    return result
            except NoTranscriptFound:
                pass

        other_language_captions_found = False
        for transcript in transcript_list:
            language_code = getattr(transcript, "language_code", "")
            if language_code in ENGLISH_TRANSCRIPT_LANGS:
                continue
            other_language_captions_found = True
            if getattr(transcript, "is_translatable", False):
                try:
                    translated = transcript.translate("en")
                    result = _fetch_transcript_candidate(
                        video_id,
                        translated,
                        provider,
                        translated=True,
                        source_language=language_code,
                    )
                    if result:
                        return result
                except Exception:
                    continue

        return _provider_failure(
            video_id,
            provider,
            REASON_TRANSCRIPT_NOT_AVAILABLE,
            other_language_captions_found=other_language_captions_found,
        )
    except TranscriptsDisabled:
        return _provider_failure(video_id, provider, REASON_TRANSCRIPTS_DISABLED)
    except (InvalidVideoId, VideoUnavailable):
        return _provider_failure(video_id, provider, REASON_VIDEO_UNAVAILABLE)
    except (IpBlocked, PoTokenRequired, RequestBlocked):
        return _provider_failure(video_id, provider, REASON_PROVIDER_BLOCKED)
    except Exception as exc:
        if _is_bot_verification_error(exc):
            return _provider_failure(video_id, provider, REASON_PROVIDER_BLOCKED)
        return _provider_failure(video_id, provider, REASON_TRANSCRIPT_NOT_AVAILABLE)


def _clean_subtitle_text(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_json3_subtitles(raw: str) -> str:
    payload = json.loads(raw)
    parts = []
    for event in payload.get("events", []):
        for segment in event.get("segs", []):
            text = _clean_subtitle_text(segment.get("utf8", ""))
            if text:
                parts.append(text)
    return " ".join(parts).strip()


def _parse_xml_subtitles(raw: str) -> str:
    root = ET.fromstring(raw)
    parts = []
    for elem in root.iter():
        tag = elem.tag.rsplit("}", 1)[-1].lower()
        if tag in {"text", "p"} and elem.text:
            text = _clean_subtitle_text(elem.text)
            if text:
                parts.append(text)
    return " ".join(parts).strip()


def _parse_vtt_subtitles(raw: str) -> str:
    parts = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped == "WEBVTT":
            continue
        if "-->" in stripped:
            continue
        if stripped.isdigit():
            continue
        if stripped.startswith(("NOTE", "STYLE", "REGION")):
            continue
        text = _clean_subtitle_text(stripped)
        if text:
            parts.append(text)
    return " ".join(parts).strip()


def _parse_subtitle_payload(raw: str, ext: str) -> str:
    ext = (ext or "").lower()
    try:
        if ext == "json3":
            return _parse_json3_subtitles(raw)
        if ext in {"srv3", "ttml"}:
            return _parse_xml_subtitles(raw)
        if ext == "vtt":
            return _parse_vtt_subtitles(raw)
    except Exception:
        return ""
    return ""


def _caption_language_priority(caption_map: dict) -> List[str]:
    languages = []
    for language in ENGLISH_TRANSCRIPT_LANGS:
        if language in caption_map:
            languages.append(language)
    for language in caption_map:
        if language.startswith("en") and language not in languages:
            languages.append(language)
    for language in caption_map:
        if language not in languages:
            languages.append(language)
    return languages


def _choose_caption_format(caption_map: dict) -> Optional[dict]:
    for language in _caption_language_priority(caption_map):
        entries = caption_map.get(language) or []
        for preferred_ext in SUBTITLE_FORMAT_PREFERENCE:
            for entry in entries:
                ext = (entry.get("ext") or "").lower()
                if ext == preferred_ext and entry.get("url"):
                    return {"language": language, "ext": ext, "url": entry["url"]}
    return None


def _choose_ytdlp_subtitle(info: dict) -> Tuple[Optional[dict], bool]:
    subtitles = info.get("subtitles") or {}
    automatic_captions = info.get("automatic_captions") or {}
    selected = _choose_caption_format(subtitles)
    if selected:
        selected["automatic"] = False
        return selected, bool(automatic_captions)
    selected = _choose_caption_format(automatic_captions)
    if selected:
        selected["automatic"] = True
        return selected, bool(subtitles or automatic_captions)
    return None, bool(subtitles or automatic_captions)


def _extract_via_ytdlp_subtitle_only_result(video_id: str) -> dict:
    provider = "yt_dlp"
    print(f"[Transcript] yt-dlp subtitle-only started video_id={video_id}", flush=True)
    try:
        import yt_dlp

        ydl_opts = {
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": YTDLP_SOCKET_TIMEOUT,
            "noplaylist": True,
        }
        cookies_path = _get_ytdlp_cookiefile()
        if cookies_path:
            ydl_opts["cookiefile"] = cookies_path
        print(f"[Transcript] yt-dlp cookies enabled: {_bool_text(cookies_path)}", flush=True)

        url = f"https://www.youtube.com/watch?v={video_id}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False, process=False)

        subtitle, any_captions = _choose_ytdlp_subtitle(info or {})
        if not subtitle:
            reason = REASON_TRANSCRIPT_NOT_AVAILABLE if any_captions else REASON_YTDLP_NO_SUBTITLES
            return _provider_failure(video_id, provider, reason)

        with httpx.Client(timeout=YTDLP_SOCKET_TIMEOUT) as client:
            resp = client.get(subtitle["url"])
            resp.raise_for_status()

        text = _parse_subtitle_payload(resp.text, subtitle["ext"])
        if not text:
            return _provider_failure(video_id, provider, REASON_YTDLP_NO_SUBTITLES)

        return _transcript_result(
            video_id=video_id,
            provider=provider,
            transcript=text,
            language=subtitle["language"],
            translated=False,
            source_language=subtitle["language"],
            target_language=None,
            transcript_type="auto" if subtitle.get("automatic") else "manual",
        )
    except Exception as exc:
        if _is_bot_verification_error(exc):
            return _provider_failure(video_id, provider, REASON_PROVIDER_BLOCKED)
        if "timed out" in str(exc).lower() or "timeout" in str(exc).lower():
            return _provider_failure(video_id, provider, REASON_YTDLP_TIMEOUT)
        if "requested format is not available" in str(exc).lower():
            return _provider_failure(video_id, provider, REASON_YTDLP_NO_SUBTITLES)
        return _provider_failure(video_id, provider, REASON_YTDLP_NO_SUBTITLES)


def _extract_via_supadata_result(video_id: str) -> dict:
    provider = "supadata"
    if not SUPADATA_API_KEY:
        return _provider_failure(video_id, provider, REASON_SUPADATA_ERROR)
    if is_supadata_cooldown_active():
        print("[Transcript] Supadata skipped because cooldown active", flush=True)
        _record_supadata_failure("cooldown_active")
        return _provider_failure(video_id, provider, REASON_SUPADATA_ERROR)

    try:
        with httpx.Client(timeout=SUPADATA_TIMEOUT) as client:
            resp = client.get(
                "https://api.supadata.ai/v1/youtube/transcript",
                params={"videoId": video_id, "lang": "en"},
                headers={"x-api-key": SUPADATA_API_KEY},
            )
        status_code = resp.status_code
        _record_supadata_status(status_code)
        print(f"[Supadata] status_code={status_code} video_id={video_id}", flush=True)

        if status_code == 200:
            payload = resp.json()
            content = payload.get("content", [])
            if isinstance(content, list):
                text = " ".join(item.get("text", "") for item in content).strip()
            else:
                text = str(content).strip()
            if text:
                print(f"[Supadata] success: video_id={video_id}", flush=True)
                return _transcript_result(
                    video_id=video_id,
                    provider=provider,
                    transcript=text,
                    language="en",
                    translated=False,
                    source_language="en",
                    target_language=None,
                    transcript_type="fallback",
                )
            _record_supadata_failure("no_transcript_content")
            print(f"[Supadata] no transcript content: video_id={video_id}", flush=True)
            return _provider_failure(video_id, provider, REASON_TRANSCRIPT_NOT_AVAILABLE)

        if status_code == 401:
            _record_supadata_failure("unauthorized_or_invalid_api_key")
            print("[Supadata] unauthorized or invalid API key", flush=True)
        elif status_code == 403:
            _record_supadata_failure("forbidden_quota_or_payment_issue")
            print("[Supadata] forbidden, quota, or payment issue", flush=True)
        elif status_code == 429:
            _record_supadata_failure("rate_limit_or_quota_exceeded")
            _activate_supadata_cooldown()
            print("[Supadata] rate limit or quota exceeded", flush=True)
            return _provider_failure(video_id, provider, REASON_SUPADATA_429)
        else:
            _record_supadata_failure(f"http_{status_code}")
            print(f"[Supadata] failed: status_code={status_code}", flush=True)
        return _provider_failure(video_id, provider, REASON_SUPADATA_ERROR)
    except httpx.TimeoutException:
        _record_provider_timeout(provider)
        _record_supadata_failure("timeout")
        print(f"[Supadata] timeout: video_id={video_id}", flush=True)
        return _provider_failure(video_id, provider, REASON_SUPADATA_ERROR)
    except Exception as exc:
        _record_supadata_failure(f"error_{type(exc).__name__}")
        print(
            f"[Transcript] extraction failed: video_id={video_id} "
            f"provider=supadata error={type(exc).__name__}",
            flush=True,
        )
        return _provider_failure(video_id, provider, REASON_SUPADATA_ERROR)


def _timeout_reason_for_provider(provider: str) -> str:
    if provider == "youtube_transcript_api":
        return REASON_YOUTUBE_TRANSCRIPT_API_TIMEOUT
    if provider == "yt_dlp":
        return REASON_YTDLP_TIMEOUT
    return REASON_SUPADATA_ERROR


def _run_result_provider_with_timeout(
    provider: str,
    method: Callable[[str], dict],
    video_id: str,
    timeout_seconds: int,
) -> dict:
    _record_provider_attempt(provider)
    result_holder: List[Optional[dict]] = [None]
    exc_holder: List[Optional[Exception]] = [None]

    def run() -> None:
        try:
            result_holder[0] = method(video_id)
        except Exception as exc:
            exc_holder[0] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=timeout_seconds)

    if t.is_alive():
        _record_provider_timeout(provider)
        reason = _timeout_reason_for_provider(provider)
        print(f"[Transcript] timeout: video_id={video_id} provider={provider}", flush=True)
        return _provider_failure(video_id, provider, reason)

    if exc_holder[0]:
        reason = REASON_PROVIDER_BLOCKED if _is_bot_verification_error(exc_holder[0]) else REASON_TRANSCRIPT_NOT_AVAILABLE
        return _provider_failure(video_id, provider, reason)

    return result_holder[0] or _provider_failure(video_id, provider, REASON_TRANSCRIPT_NOT_AVAILABLE)


def _extract_provider_for_candidates(
    candidates: List,
    provider: str,
    method: Callable[[str], dict],
    max_concurrent: int,
    timeout_seconds: int,
    stats: dict,
) -> Optional[dict]:
    executor = ThreadPoolExecutor(max_workers=max(1, max_concurrent))
    pending = {}
    remaining = iter(candidates)
    shutdown_started = False

    def submit_next() -> bool:
        try:
            candidate = next(remaining)
        except StopIteration:
            return False
        video_id = _candidate_video_id(candidate)
        if not video_id:
            return submit_next()
        pending[executor.submit(
            _run_result_provider_with_timeout,
            provider,
            method,
            video_id,
            timeout_seconds,
        )] = video_id
        return True

    try:
        for _ in range(min(max(1, max_concurrent), len(candidates))):
            submit_next()

        while pending:
            done, _ = wait(
                pending.keys(),
                timeout=timeout_seconds + 1,
                return_when=FIRST_COMPLETED,
            )
            if not done:
                continue

            success = None
            for future in done:
                video_id = pending.pop(future)
                try:
                    result = future.result()
                except Exception:
                    result = _provider_failure(video_id, provider, REASON_TRANSCRIPT_NOT_AVAILABLE)

                stats["checked_candidates"] += 1
                if result.get("ok"):
                    success = result
                    stats["success"] = 1
                    break

                reason = result.get("reason", REASON_TRANSCRIPT_NOT_AVAILABLE)
                _record_mode_failure(stats, video_id, reason)
                _record_failure(video_id, reason)
                if result.get("other_language_captions_found"):
                    _increment_stat(stats, "other_language_captions_found")

            if success:
                for other in pending:
                    other.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                shutdown_started = True
                return success

            if not pending:
                for _ in range(min(max(1, max_concurrent), len(candidates))):
                    submit_next()

        return None
    finally:
        if not shutdown_started:
            executor.shutdown(wait=True, cancel_futures=True)


def extract_fast_mode(candidates: List) -> dict:
    fast_candidates = list(candidates)[:FAST_CANDIDATE_LIMIT]
    stats = _new_mode_stats("fast", fast_candidates)
    print("[Transcript] fast mode started", flush=True)
    print(f"[Transcript] fast candidate count: {len(fast_candidates)}", flush=True)
    print(f"[Transcript] fast parallel limit: {FAST_PARALLEL_LIMIT}", flush=True)
    print(f"[Transcript] fast timeout seconds: {FAST_PROVIDER_TIMEOUT}", flush=True)

    result = _extract_provider_for_candidates(
        fast_candidates,
        "youtube_transcript_api",
        _extract_youtube_transcript_api_result,
        FAST_PARALLEL_LIMIT,
        FAST_PROVIDER_TIMEOUT,
        stats,
    )
    if result:
        result["mode_used"] = "fast"
        result["stats"] = stats
        print(
            f"[Transcript] success provider=youtube_transcript_api mode=fast "
            f"video_id={result['video_id']}",
            flush=True,
        )
        return result

    return {"ok": False, "reason": "fast_mode_failed", "stats": stats}


def should_enter_deep_mode(stats: dict) -> bool:
    checked_threshold = min(25, max(1, stats.get("candidate_count", 0)))
    return (
        stats.get("checked_candidates", 0) >= checked_threshold
        or stats.get(REASON_TRANSCRIPT_NOT_AVAILABLE, 0) >= 15
        or stats.get(REASON_YOUTUBE_TRANSCRIPT_API_TIMEOUT, 0) >= 5
        or stats.get(REASON_PROVIDER_BLOCKED, 0) > 0
        or stats.get("other_language_captions_found", 0) > 0
    ) and stats.get("success", 0) == 0


def extract_deep_mode(candidates: List, previous_stats: Optional[dict] = None) -> dict:
    deep_candidates = list(candidates)[:DEEP_CANDIDATE_LIMIT]
    stats = _new_mode_stats("deep", deep_candidates)
    print("[Transcript] entering deep mode", flush=True)
    print(f"[Transcript] deep candidate count: {len(deep_candidates)}", flush=True)
    print(f"[Transcript] deep parallel limit: {DEEP_PARALLEL_LIMIT}", flush=True)

    ytdlp_candidates = deep_candidates[:DEEP_YTDLP_LIMIT]
    print(f"[Transcript] yt-dlp candidate count: {len(ytdlp_candidates)}", flush=True)
    print(f"[Transcript] yt-dlp timeout seconds: {YTDLP_SOCKET_TIMEOUT}", flush=True)
    result = _extract_provider_for_candidates(
        ytdlp_candidates,
        "yt_dlp",
        _extract_via_ytdlp_subtitle_only_result,
        DEEP_PARALLEL_LIMIT,
        YTDLP_SOCKET_TIMEOUT + 3,
        stats,
    )
    if result:
        result["mode_used"] = "deep"
        result["stats"] = stats
        print(
            f"[Transcript] success provider=yt_dlp mode=deep "
            f"video_id={result['video_id']}",
            flush=True,
        )
        return result

    if not SUPADATA_API_KEY:
        stats["whisper_todo"] = "not_configured"
        return {"ok": False, "reason": "deep_mode_failed", "stats": stats}

    if is_supadata_cooldown_active():
        print("[Transcript] Supadata skipped because cooldown active", flush=True)
        _record_supadata_failure("cooldown_active")
        stats["whisper_todo"] = "not_configured"
        return {"ok": False, "reason": "deep_mode_failed", "stats": stats}

    for candidate in deep_candidates[:DEEP_SUPADATA_LIMIT]:
        video_id = _candidate_video_id(candidate)
        result = _run_result_provider_with_timeout(
            "supadata",
            _extract_via_supadata_result,
            video_id,
            SUPADATA_TIMEOUT + 1,
        )
        stats["checked_candidates"] += 1
        if result.get("ok"):
            result["mode_used"] = "deep"
            result["stats"] = stats
            print(
                f"[Transcript] success provider=supadata mode=deep "
                f"video_id={result['video_id']}",
                flush=True,
            )
            return result

        reason = result.get("reason", REASON_SUPADATA_ERROR)
        _record_mode_failure(stats, video_id, reason)
        if reason == REASON_SUPADATA_429:
            break

    stats["whisper_todo"] = "not_configured"
    return {"ok": False, "reason": "deep_mode_failed", "stats": stats}


def extract_transcript_auto(candidates: List, content_type: Optional[str] = None) -> dict:
    print("[Transcript] auto mode started", flush=True)
    if content_type:
        print(f"[Transcript] content_type={content_type}", flush=True)
    print(f"[Transcript] Supadata enabled: {_bool_text(SUPADATA_API_KEY)}", flush=True)
    resetTranscriptProviderDiagnostics()
    candidates = list(candidates)
    candidate_ids = [_candidate_video_id(candidate) for candidate in candidates]
    with _failure_lock:
        for video_id in candidate_ids:
            _last_failure_reasons.pop(video_id, None)

    fast_result = extract_fast_mode(candidates[:FAST_CANDIDATE_LIMIT])
    if fast_result.get("ok"):
        return fast_result

    print(
        f"[Transcript] fast mode failed stats={json.dumps(fast_result['stats'], sort_keys=True)}",
        flush=True,
    )

    deep_result = None
    deep_attempted = should_enter_deep_mode(fast_result["stats"])
    if deep_attempted:
        deep_candidates = candidates[
            DEEP_CANDIDATE_START:DEEP_CANDIDATE_START + DEEP_CANDIDATE_LIMIT
        ]
        if not deep_candidates:
            deep_candidates = candidates[:DEEP_CANDIDATE_LIMIT]
        deep_result = extract_deep_mode(deep_candidates, previous_stats=fast_result["stats"])
        if deep_result.get("ok"):
            return deep_result

    diagnostics = getTranscriptProviderDiagnostics()
    failed = {
        "ok": False,
        "reason": "no_usable_transcript_found",
        "fast_stats": fast_result["stats"],
        "deep_attempted": deep_attempted,
        "deep_stats": deep_result.get("stats") if deep_result else None,
        "provider_attempt_counts": diagnostics["provider_attempt_counts"],
        "provider_timeout_counts": diagnostics["provider_timeout_counts"],
        "supadata_status_counts": diagnostics["supadata_status_counts"],
        "supadata_failure_reason_counts": diagnostics["supadata_failure_reason_counts"],
        "attempted_video_ids": candidate_ids,
    }
    print(
        f"[Transcript] failed reason=no_usable_transcript_found "
        f"stats={json.dumps(failed, sort_keys=True)}",
        flush=True,
    )
    return failed


def _extract_via_transcript_api(video_id: str) -> Optional[Tuple[str, str]]:
    """Try youtube-transcript-api. Returns (text, type) or None."""
    result = _extract_youtube_transcript_api_result(video_id)
    if result.get("ok"):
        return result["transcript"], result.get("transcript_type", "auto")

    reason = result.get("reason", REASON_TRANSCRIPT_NOT_AVAILABLE)
    if reason == REASON_PROVIDER_BLOCKED:
        _record_failure(video_id, FAILURE_BOT_VERIFICATION)
    elif reason == REASON_YOUTUBE_TRANSCRIPT_API_TIMEOUT:
        _record_failure(video_id, FAILURE_TRANSCRIPT_TIMEOUT)
        _record_provider_timeout("youtube_transcript_api")
    else:
        _record_failure(video_id, FAILURE_TRANSCRIPT_NOT_AVAILABLE)
    return None


def _extract_via_ytdlp(video_id: str) -> Optional[Tuple[str, str]]:
    """Try yt-dlp subtitle-only extraction. Returns (text, type) or None."""
    result = _extract_via_ytdlp_subtitle_only_result(video_id)
    if result.get("ok"):
        return result["transcript"], result.get("transcript_type", "auto")

    reason = result.get("reason", REASON_YTDLP_NO_SUBTITLES)
    if reason == REASON_PROVIDER_BLOCKED:
        _record_failure(video_id, FAILURE_BOT_VERIFICATION)
    elif reason == REASON_YTDLP_TIMEOUT:
        _record_failure(video_id, FAILURE_TRANSCRIPT_TIMEOUT)
        _record_provider_timeout("yt_dlp")
    else:
        _record_failure(video_id, FAILURE_TRANSCRIPT_NOT_AVAILABLE)
    return None


def _extract_via_supadata(video_id: str) -> Optional[Tuple[str, str]]:
    """Supadata API fallback. Returns (text, 'fallback') or None."""
    if not SUPADATA_API_KEY:
        return None
    try:
        with httpx.Client(timeout=SUPADATA_TIMEOUT) as client:
            resp = client.get(
                "https://api.supadata.ai/v1/youtube/transcript",
                params={"videoId": video_id, "lang": "en"},
                headers={"x-api-key": SUPADATA_API_KEY},
            )
            status_code = resp.status_code
            _record_supadata_status(status_code)
            print(f"[Supadata] status_code={status_code} video_id={video_id}", flush=True)

            if status_code == 200:
                payload = resp.json()
                content = payload.get("content", [])
                if isinstance(content, list):
                    text = " ".join(item.get("text", "") for item in content).strip()
                else:
                    text = str(content).strip()
                if text:
                    print(f"[Supadata] success: video_id={video_id}", flush=True)
                    return text, "fallback"
                _record_supadata_failure("no_transcript_content")
                _record_failure(video_id, FAILURE_TRANSCRIPT_NOT_AVAILABLE)
                print(f"[Supadata] no transcript content: video_id={video_id}", flush=True)
                return None

            if status_code == 401:
                _record_supadata_failure("unauthorized_or_invalid_api_key")
                print("[Supadata] unauthorized or invalid API key", flush=True)
            elif status_code == 403:
                _record_supadata_failure("forbidden_quota_or_payment_issue")
                print("[Supadata] forbidden, quota, or payment issue", flush=True)
            elif status_code == 429:
                _record_supadata_failure("rate_limit_or_quota_exceeded")
                _activate_supadata_cooldown()
                print("[Supadata] rate limit or quota exceeded", flush=True)
            else:
                _record_supadata_failure(f"http_{status_code}")
                print(f"[Supadata] failed: status_code={status_code}", flush=True)
    except httpx.TimeoutException:
        _record_provider_timeout("supadata")
        _record_supadata_failure("timeout")
        print(f"[Supadata] timeout: video_id={video_id}", flush=True)
    except Exception as exc:
        reason = _classify_error(exc)
        if reason == FAILURE_TRANSCRIPT_TIMEOUT:
            _record_provider_timeout("supadata")
            _record_supadata_failure("timeout")
            print(f"[Transcript] timeout: video_id={video_id} provider=supadata", flush=True)
        else:
            _record_supadata_failure(f"error_{type(exc).__name__}")
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
    methods = [
        ("youtube_transcript_api", _extract_via_transcript_api),
        ("yt_dlp", _extract_via_ytdlp),
    ]
    if SUPADATA_API_KEY and not is_supadata_cooldown_active():
        methods.append(("supadata", _extract_via_supadata))

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
    _record_provider_attempt(provider)
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
        if provider != "supadata":
            _record_failure(video_id, FAILURE_TRANSCRIPT_TIMEOUT)
        _record_provider_timeout(provider)
        if provider == "supadata":
            _record_supadata_failure("timeout")
        print(f"[Transcript] timeout: video_id={video_id} provider={provider}", flush=True)
        return None

    if exc_holder[0]:
        reason = _classify_error(exc_holder[0])
        if provider != "supadata":
            _record_failure(video_id, reason)
        if reason == FAILURE_BOT_VERIFICATION:
            print(f"[Transcript] yt-dlp bot verification blocked: video_id={video_id}", flush=True)
        elif reason == FAILURE_TRANSCRIPT_TIMEOUT:
            _record_provider_timeout(provider)
            if provider == "supadata":
                _record_supadata_failure("timeout")
            print(f"[Transcript] timeout: video_id={video_id} provider={provider}", flush=True)
        else:
            print(
                f"[Transcript] extraction failed: video_id={video_id} "
                f"provider={provider} error={type(exc_holder[0]).__name__}",
                flush=True,
            )
        return None

    if not result_holder[0] and provider != "supadata":
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
    ytdlp_limit: Optional[int] = None,
) -> Dict[str, Tuple[str, str]]:
    """
    Extract transcripts for scored videos. Returns after the first provider success.
    """
    video_ids = list(video_ids)
    results: Dict[str, Tuple[str, str]] = {}
    with _failure_lock:
        for vid_id in video_ids:
            _last_failure_reasons.pop(vid_id, None)
    resetTranscriptProviderDiagnostics()

    print(f"[Transcript] candidate count: {len(video_ids)}", flush=True)
    print(f"[Transcript] parallel limit: {max_concurrent}", flush=True)
    print(f"[Transcript] timeout seconds: {TRANSCRIPT_TIMEOUT}", flush=True)
    print(f"[Transcript] Supadata enabled: {_bool_text(SUPADATA_API_KEY)}", flush=True)

    for provider, method in [
        ("youtube_transcript_api", _extract_via_transcript_api),
    ]:
        results = _extract_provider_parallel(video_ids, provider, method, max_concurrent)
        if results:
            return results

    ytdlp_ids = video_ids[:ytdlp_limit] if ytdlp_limit else video_ids
    print(f"[Transcript] yt-dlp candidate count: {len(ytdlp_ids)}", flush=True)
    results = _extract_provider_parallel(ytdlp_ids, "yt_dlp", _extract_via_ytdlp, max_concurrent)
    if results:
        return results

    if SUPADATA_API_KEY and not is_supadata_cooldown_active():
        supadata_ids = video_ids[:supadata_limit] if supadata_limit else video_ids
        print(f"[Transcript] Supadata candidate count: {len(supadata_ids)}", flush=True)
        results = _extract_provider_parallel(
            supadata_ids,
            "supadata",
            _extract_via_supadata,
            max_concurrent,
        )
        if results:
            return results
    elif SUPADATA_API_KEY:
        print("[Transcript] Supadata skipped because cooldown active", flush=True)

    return results


def extract_supadata_batch(video_ids: List[str]) -> Dict[str, Tuple[str, str]]:
    """Try Supadata for a list of video IDs. Returns successes."""
    results: Dict[str, Tuple[str, str]] = {}
    for vid_id in video_ids:
        _record_provider_attempt("supadata")
        result = _extract_via_supadata(vid_id)
        if result:
            _log_provider_success("supadata", vid_id)
            results[vid_id] = result
    return results
