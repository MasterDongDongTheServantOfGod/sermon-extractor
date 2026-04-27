import os
import re
import json
from datetime import datetime
from typing import Dict, List

import httpx
from jinja2 import Template

_ARTICLE_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{ seo_title or title }}</title>
  <meta name="description" content="{{ meta_description }}">
  <meta property="og:title" content="{{ title }}">
  <meta property="og:description" content="{{ meta_description }}">
  {% if image_url %}<meta property="og:image" content="{{ image_url }}">{% endif %}
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    body {
      font-family: Georgia, 'Times New Roman', serif;
      max-width: 780px;
      margin: 0 auto;
      padding: 24px 20px 60px;
      color: #222;
      background: #fff;
      line-height: 1.7;
    }
    a { color: #1a6b3a; }
    h1 { font-size: 2em; line-height: 1.25; margin: 0 0 0.4em; }
    .deck { font-size: 1.15em; color: #444; font-style: italic; margin-bottom: 1em; border-left: 3px solid #1a6b3a; padding-left: 12px; }
    .meta { font-size: 0.88em; color: #777; margin-bottom: 1.5em; }
    .meta strong { color: #444; }
    .scripture-banner {
      background: #f6f3ea;
      border-left: 4px solid #a07a2a;
      padding: 14px 16px;
      margin: 1.5em 0;
      font-style: italic;
      font-size: 1.05em;
    }
    img.hero { width: 100%; max-height: 420px; object-fit: cover; border-radius: 4px; margin-bottom: 1.2em; }
    .article-body { font-size: 1.05em; }
    .article-body p { margin: 0 0 1.2em; }
    .article-body blockquote {
      border-left: 3px solid #ccc;
      margin: 1.5em 0;
      padding: 0.4em 1em;
      color: #555;
      font-style: italic;
    }
    .article-body h2, .article-body h3 { font-family: Arial, sans-serif; margin: 1.6em 0 0.5em; }
    .tags { margin: 2em 0 1em; display: flex; flex-wrap: wrap; gap: 6px; }
    .tag { background: #eef; color: #336; padding: 3px 10px; border-radius: 20px; font-size: 0.82em; font-family: Arial, sans-serif; }
    .source-box { background: #f5f5f5; border: 1px solid #ddd; padding: 14px 16px; border-radius: 4px; font-size: 0.9em; font-family: Arial, sans-serif; margin-top: 2em; }
    .source-box strong { display: block; margin-bottom: 4px; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 0.78em; font-family: Arial, sans-serif; font-weight: bold; margin-left: 8px; vertical-align: middle; }
    .badge-low { background: #d4edda; color: #155724; }
    .badge-med { background: #fff3cd; color: #856404; }
    .badge-high { background: #f8d7da; color: #721c24; }
  </style>
</head>
<body>
  {% if image_url %}
  <img class="hero" src="{{ image_url }}" alt="{{ title }}">
  {% endif %}

  <h1>{{ title }}</h1>

  {% if deck %}
  <p class="deck">{{ deck }}</p>
  {% endif %}

  <div class="meta">
    <strong>{{ pastor_name }}</strong>{% if church_name %} &mdash; {{ church_name }}{% endif %}
    {% if published_date %} &nbsp;|&nbsp; {{ published_date }}{% endif %}
    <span class="badge badge-{{ risk_badge }}">Risk: {{ risk_level }}</span>
    &nbsp;<span class="badge" style="background:#e2e8f0;color:#334;">SEO {{ seo_score }}</span>
  </div>

  {% if primary_scripture %}
  <div class="scripture-banner">&#128214; {{ primary_scripture }}</div>
  {% endif %}

  <div class="article-body">
    {{ article_body_html | safe }}
  </div>

  {% if tags %}
  <div class="tags">
    {% for tag in tags %}<span class="tag">{{ tag }}</span>{% endfor %}
  </div>
  {% endif %}

  <div class="source-box">
    <strong>Source</strong>
    <a href="{{ video_url }}" target="_blank" rel="noopener">{{ sermon_title }}</a><br>
    Pastor: {{ pastor_name }}{% if church_name %}, {{ church_name }}{% endif %}
  </div>
</body>
</html>
"""


def _slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-")[:60]


def _body_to_html(body: str) -> str:
    """Convert plain text / light markdown to HTML."""
    lines = body.split("\n")
    html_parts = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("### "):
            html_parts.append(f"<h3>{stripped[4:]}</h3>")
        elif stripped.startswith("## "):
            html_parts.append(f"<h2>{stripped[3:]}</h2>")
        elif stripped.startswith("> "):
            html_parts.append(f"<blockquote>{stripped[2:]}</blockquote>")
        elif stripped.startswith('"') and stripped.endswith('"'):
            html_parts.append(f"<blockquote>{stripped}</blockquote>")
        else:
            html_parts.append(f"<p>{stripped}</p>")
    return "\n".join(html_parts)


def _download_image(url: str, dest: str) -> bool:
    try:
        with httpx.Client(timeout=12.0, follow_redirects=True) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                with open(dest, "wb") as fh:
                    fh.write(resp.content)
                return True
    except Exception:
        pass
    return False


def _update_index(entry: dict) -> None:
    index_path = os.path.join("data", "articles-index.json")
    try:
        with open(index_path, "r", encoding="utf-8") as fh:
            index = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        index = {"articles": []}

    index["articles"] = [a for a in index["articles"] if a.get("id") != entry.get("id")]
    index["articles"].insert(0, entry)
    index["updated_at"] = datetime.utcnow().isoformat()

    with open(index_path, "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2, ensure_ascii=False)


def publish_article(
    article_id: int,
    title: str,
    deck: str,
    article_body: str,
    primary_scripture: str,
    seo_title: str,
    meta_description: str,
    tags: List[str],
    pastor_name: str,
    church_name: str,
    sermon_title: str,
    video_url: str,
    thumbnail_url: str,
    published_date: str,
    seo_score: int = 0,
    risk_level: str = "LOW",
) -> Dict:
    """Generate static HTML + metadata for a published article. Returns {slug, html_path}."""
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    slug = _slugify(title)
    dir_name = f"{timestamp}_{slug}"
    article_dir = os.path.join("articles", dir_name)
    os.makedirs(article_dir, exist_ok=True)

    # Download thumbnail
    image_relative = ""
    if thumbnail_url:
        img_path = os.path.join(article_dir, "image.jpg")
        if _download_image(thumbnail_url, img_path):
            image_relative = f"/articles/{dir_name}/image.jpg"

    article_body_html = _body_to_html(article_body)
    risk_badge = {"LOW": "low", "MEDIUM": "med", "HIGH": "high"}.get(risk_level, "med")

    template = Template(_ARTICLE_HTML)
    html = template.render(
        title=title,
        deck=deck,
        article_body_html=article_body_html,
        primary_scripture=primary_scripture,
        seo_title=seo_title or title,
        meta_description=meta_description,
        tags=tags,
        pastor_name=pastor_name,
        church_name=church_name,
        sermon_title=sermon_title,
        video_url=video_url,
        image_url=image_relative,
        published_date=published_date,
        seo_score=seo_score,
        risk_level=risk_level,
        risk_badge=risk_badge,
    )

    html_file = os.path.join(article_dir, "article.html")
    with open(html_file, "w", encoding="utf-8") as fh:
        fh.write(html)

    metadata = {
        "id": article_id,
        "title": title,
        "slug": slug,
        "primary_scripture": primary_scripture,
        "pastor_name": pastor_name,
        "church_name": church_name,
        "published_date": published_date,
        "html_path": f"/articles/{dir_name}/article.html",
        "image_url": image_relative,
        "tags": tags,
        "meta_description": meta_description,
        "seo_score": seo_score,
        "risk_level": risk_level,
    }

    with open(os.path.join(article_dir, "metadata.json"), "w", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2, ensure_ascii=False)

    _update_index(metadata)

    return {"slug": slug, "html_path": f"/articles/{dir_name}/article.html"}


def rebuild_article_files(
    article_id: int,
    dir_name: str,
    title: str,
    deck: str,
    article_body: str,
    primary_scripture: str,
    seo_title: str,
    meta_description: str,
    tags: List[str],
    pastor_name: str,
    church_name: str,
    sermon_title: str,
    video_url: str,
    thumbnail_url: str,
    published_date: str,
    seo_score: int = 0,
    risk_level: str = "LOW",
) -> Dict:
    """Recreate static files for an existing article using its known dir_name."""
    article_dir = os.path.join("articles", dir_name)
    os.makedirs(article_dir, exist_ok=True)

    image_relative = ""
    if thumbnail_url:
        img_path = os.path.join(article_dir, "image.jpg")
        if not os.path.exists(img_path):
            _download_image(thumbnail_url, img_path)
        if os.path.exists(img_path):
            image_relative = f"/articles/{dir_name}/image.jpg"

    article_body_html = _body_to_html(article_body)
    risk_badge = {"LOW": "low", "MEDIUM": "med", "HIGH": "high"}.get(risk_level, "med")

    template = Template(_ARTICLE_HTML)
    html = template.render(
        title=title, deck=deck, article_body_html=article_body_html,
        primary_scripture=primary_scripture, seo_title=seo_title or title,
        meta_description=meta_description, tags=tags,
        pastor_name=pastor_name, church_name=church_name,
        sermon_title=sermon_title, video_url=video_url,
        image_url=image_relative, published_date=published_date,
        seo_score=seo_score, risk_level=risk_level, risk_badge=risk_badge,
    )

    with open(os.path.join(article_dir, "article.html"), "w", encoding="utf-8") as fh:
        fh.write(html)

    return {
        "id": article_id,
        "title": title,
        "slug": dir_name.split("_", 2)[-1] if dir_name.count("_") >= 2 else dir_name,
        "primary_scripture": primary_scripture,
        "pastor_name": pastor_name,
        "church_name": church_name,
        "published_date": published_date,
        "html_path": f"/articles/{dir_name}/article.html",
        "image_url": image_relative,
        "tags": tags,
        "meta_description": meta_description,
        "seo_score": seo_score,
        "risk_level": risk_level,
    }
