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
    components = []

    title_lc = (title or "").lower()
    body_lc = (article_body or "").lower()
    first_para = body_lc[:600]
    keyword = keywords[0].lower() if keywords else ""

    # 1. Keyword in title (15 pts)
    if keyword and keyword in title_lc:
        score += 15
        checks["keyword_in_title"] = True
        components.append({"key": "keyword_in_title", "label": "Title Keyword", "score": 15, "max": 15})
    else:
        checks["keyword_in_title"] = False
        components.append({"key": "keyword_in_title", "label": "Title Keyword", "score": 0, "max": 15})

    # 2. Keyword in first paragraph (15 pts)
    if keyword and keyword in first_para:
        score += 15
        checks["keyword_in_first_paragraph"] = True
        components.append({"key": "keyword_in_first_paragraph", "label": "First Paragraph", "score": 15, "max": 15})
    else:
        checks["keyword_in_first_paragraph"] = False
        components.append({"key": "keyword_in_first_paragraph", "label": "First Paragraph", "score": 0, "max": 15})

    # 3. Meta description (15 pts)
    if meta_description and len(meta_description.strip()) >= 50:
        score += 15
        checks["meta_description"] = True
        components.append({"key": "meta_description", "label": "Meta Description", "score": 15, "max": 15})
    else:
        checks["meta_description"] = False
        components.append({"key": "meta_description", "label": "Meta Description", "score": 0, "max": 15})

    # 4. Content length (20 pts)
    word_count = len((article_body or "").split())
    if word_count >= 400:
        score += 20
        checks["content_length"] = f"{word_count} words"
        components.append({"key": "content_length", "label": "Length", "score": 20, "max": 20})
    elif word_count >= 250:
        score += 10
        checks["content_length"] = f"{word_count} words (short)"
        components.append({"key": "content_length", "label": "Length", "score": 10, "max": 20})
    else:
        checks["content_length"] = f"{word_count} words (too short)"
        components.append({"key": "content_length", "label": "Length", "score": 0, "max": 20})

    # 5. Sources present (10 pts)
    if sources:
        score += 10
        checks["sources"] = f"{len(sources)} source(s)"
        components.append({"key": "sources", "label": "Sources", "score": 10, "max": 10})
    else:
        checks["sources"] = "No sources"
        components.append({"key": "sources", "label": "Sources", "score": 0, "max": 10})

    # 6. Heading structure (15 pts) — look for lines starting with ## or bold headers
    heading_count = len(re.findall(r"#{1,3}\s|\*\*[^*]{3,50}\*\*", article_body or ""))
    if heading_count >= 3:
        score += 15
        checks["headings"] = f"{heading_count} headings"
        components.append({"key": "headings", "label": "Headings", "score": 15, "max": 15})
    elif heading_count >= 1:
        score += 7
        checks["headings"] = f"{heading_count} heading(s)"
        components.append({"key": "headings", "label": "Headings", "score": 7, "max": 15})
    else:
        checks["headings"] = "No headings detected"
        components.append({"key": "headings", "label": "Headings", "score": 0, "max": 15})

    # 7. Low repetition (10 pts)
    words = body_lc.split()
    if words:
        unique_ratio = len(set(words)) / len(words)
        if unique_ratio > 0.38:
            score += 10
            checks["repetition"] = f"Good ({unique_ratio:.0%} unique)"
            components.append({"key": "repetition", "label": "Low Repetition", "score": 10, "max": 10})
        else:
            checks["repetition"] = f"High repetition ({unique_ratio:.0%} unique)"
            components.append({"key": "repetition", "label": "Low Repetition", "score": 0, "max": 10})
    else:
        checks["repetition"] = "No content"
        components.append({"key": "repetition", "label": "Low Repetition", "score": 0, "max": 10})

    for component in components:
        component["percent"] = round(component["score"] / component["max"] * 100) if component["max"] else 0

    return {"seo_score": min(score, 100), "checks": checks, "components": components}
