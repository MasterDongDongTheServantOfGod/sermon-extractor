import re
from datetime import datetime, timezone
from typing import Dict, Tuple

SCRIPTURE_PATTERN = re.compile(
    r"\b(genesis|exodus|leviticus|numbers|deuteronomy|joshua|judges|ruth|"
    r"samuel|kings|chronicles|ezra|nehemiah|esther|job|psalm|psalms|proverbs|"
    r"ecclesiastes|isaiah|jeremiah|lamentations|ezekiel|daniel|hosea|joel|amos|"
    r"obadiah|jonah|micah|nahum|habakkuk|zephaniah|haggai|zechariah|malachi|"
    r"matthew|mark|luke|john|acts|romans|corinthians|galatians|ephesians|"
    r"philippians|colossians|thessalonians|timothy|titus|philemon|hebrews|"
    r"james|peter|jude|revelation)\s+\d+:\d+",
    re.IGNORECASE,
)

SERIES_PATTERN = re.compile(
    r"\b(part|pt\.?|ep\.?|episode|week|session|series)\s*[1-9]\b",
    re.IGNORECASE,
)


def passes_hard_filters(video: Dict) -> Tuple[bool, str]:
    """Check mandatory pass/fail criteria. Returns (passes, reason)."""
    duration_min = video.get("duration_seconds", 0) / 60

    if duration_min < 10:
        return False, "Video too short (< 10 min)"
    if duration_min > 120:
        return False, "Video too long (> 2 hours)"

    return True, ""


def score_video(video: Dict, is_new: bool = True) -> float:
    """Score a video candidate. Max possible ~108 points."""
    score = 0.0
    now = datetime.now(timezone.utc)

    published_at = video.get("published_at", now)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)
    age_days = (now - published_at).days

    # ① Recency (30 pts)
    if age_days <= 7:
        score += 30
    elif age_days <= 14:
        score += 20
    elif age_days <= 30:
        score += 10

    # ② Views (25 pts)
    views = video.get("view_count", 0)
    if views >= 500_000:
        score += 25
    elif views >= 100_000:
        score += 15
    elif views >= 10_000:
        score += 5

    # ③ Transcript quality (20 pts)
    transcript_type = video.get("transcript_type")
    if transcript_type == "manual":
        score += 20
    elif transcript_type == "auto" or video.get("has_captions"):
        score += 10

    # ④ Duration (15 pts)
    duration_min = video.get("duration_seconds", 0) / 60
    if 25 <= duration_min <= 45:
        score += 15
    elif 45 < duration_min <= 70:
        score += 10
    elif 10 <= duration_min < 25:
        score += 5
    elif duration_min > 70:
        score += 3

    # ⑤ New video (10 pts)
    if is_new:
        score += 10

    # Bonus
    if video.get("comment_count", 0) > 1000:
        score += 5

    text = (video.get("title", "") + " " + video.get("description", "")).strip()
    if SCRIPTURE_PATTERN.search(text):
        score += 5
    if SERIES_PATTERN.search(text) and re.search(r"\bpart\s*1\b|\bpt\.?\s*1\b|\bep\.?\s*1\b", text, re.IGNORECASE):
        score += 3

    return score
