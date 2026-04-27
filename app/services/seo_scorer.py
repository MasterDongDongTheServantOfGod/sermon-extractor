import re
from typing import Dict, List


def score_seo(
    title: str,
    article_body: str,
    meta_description: str,
    keywords: List[str],
    sources: List[str],
    primary_scripture: str,
) -> Dict:
    """
    Score SEO quality 0–100.
    Returns {seo_score, checks}.
    """
    score = 0
    checks: Dict[str, object] = {}

    title_lc = (title or "").lower()
    body_lc = (article_body or "").lower()
    first_para = body_lc[:600]
    keyword = keywords[0].lower() if keywords else ""

    # 1. Keyword in title (15 pts)
    if keyword and keyword in title_lc:
        score += 15
        checks["keyword_in_title"] = True
    else:
        checks["keyword_in_title"] = False

    # 2. Keyword in first paragraph (15 pts)
    if keyword and keyword in first_para:
        score += 15
        checks["keyword_in_first_paragraph"] = True
    else:
        checks["keyword_in_first_paragraph"] = False

    # 3. Meta description (15 pts)
    if meta_description and len(meta_description.strip()) >= 50:
        score += 15
        checks["meta_description"] = True
    else:
        checks["meta_description"] = False

    # 4. Content length (20 pts)
    word_count = len((article_body or "").split())
    if word_count >= 400:
        score += 20
        checks["content_length"] = f"{word_count} words"
    elif word_count >= 250:
        score += 10
        checks["content_length"] = f"{word_count} words (short)"
    else:
        checks["content_length"] = f"{word_count} words (too short)"

    # 5. Sources present (10 pts)
    if sources:
        score += 10
        checks["sources"] = f"{len(sources)} source(s)"
    else:
        checks["sources"] = "No sources"

    # 6. Heading structure (15 pts) — look for lines starting with ## or bold headers
    heading_count = len(re.findall(r"#{1,3}\s|\*\*[^*]{3,50}\*\*", article_body or ""))
    if heading_count >= 3:
        score += 15
        checks["headings"] = f"{heading_count} headings"
    elif heading_count >= 1:
        score += 7
        checks["headings"] = f"{heading_count} heading(s)"
    else:
        checks["headings"] = "No headings detected"

    # 7. Low repetition (10 pts)
    words = body_lc.split()
    if words:
        unique_ratio = len(set(words)) / len(words)
        if unique_ratio > 0.38:
            score += 10
            checks["repetition"] = f"Good ({unique_ratio:.0%} unique)"
        else:
            checks["repetition"] = f"High repetition ({unique_ratio:.0%} unique)"
    else:
        checks["repetition"] = "No content"

    return {"seo_score": min(score, 100), "checks": checks}
