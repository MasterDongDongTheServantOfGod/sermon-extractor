import os
import json
from typing import Dict, List, Optional

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

_INPUT_COST_PER_1M = 0.40
_OUTPUT_COST_PER_1M = 1.60

_NEWS_SYSTEM = """\
You are a professional Christian news journalist writing in the style of The Christian Post.

Write a news article following this exact structure:
1. Headline: "Pastor [Name] [says/urges/warns] [central claim] in sermon on [Bible theme]"
2. Deck: One strong quote or one-sentence summary
3. Lead paragraph: who, what, when, where
4. Context/Background paragraph
5. Scripture Explanation paragraph
6. Main Message paragraph with Quote 1
7. Development paragraph with Quote 2
8. Application paragraph
9. Conclusion paragraph
10. Source attribution

Be factual, objective, and journalistic. Target ~{word_count} words.

Return ONLY a JSON object with fields:
title, deck, article_body, primary_scripture, seo_title, meta_description, tags (string array)
"""

_BLOG_SYSTEM = """\
You are a Christian devotional blog writer creating warm, practical content.

Write a devotional blog post following this exact structure:
1. Title: application-centered (e.g. "When Life Feels Impossible, God Is Still Sovereign")
2. Subtitle: short sentence summarizing the spiritual lesson
3. Today's Scripture: primary verse (ESV)
4. Introduction: start with a relatable human struggle or spiritual question
5. Sermon Summary: introduce pastor, sermon title, and main point
6. Biblical Reflection: explain the passage in devotional language
7. Key Insight: main theological truth
8. Quote from the Sermon: 1-3 strong quotes
9. Life Application: how readers can apply this truth daily
10. Reflection Questions: 3-5 questions for meditation
11. Prayer: short closing prayer
12. Conclusion: one encouraging sentence
13. Source attribution

Keep it warm, personal, and application-focused. Target ~{word_count} words.

Return ONLY a JSON object with fields:
title, deck, article_body, primary_scripture, seo_title, meta_description, tags (string array)
"""

_BEFORE_WATCH_SYSTEM = """\
You are a Christian editorial writer creating a "Before You Watch" guide.

The goal is not to fully summarize the sermon. The goal is to help readers decide
why the original video is worth watching and what to listen for when they open it.

Write the piece following this exact structure:
1. Title: "Why This Sermon Is Worth Watching: [Pastor Name] on [Theme]"
2. Why this sermon is worth watching
3. Recommended for viewers who...
4. The central question this sermon raises
5. Insight for modern Christian life
6. Community Response
7. What to watch for in the original sermon
8. Reflection Questions
9. Watch the Original Sermon

For the Community Response section, use the provided YouTube comment notes only
as aggregate audience context. Do not quote comments verbatim. Do not mention
usernames. Do not write "one user said" or similar attribution. Describe the
shared sentiment, points of resonance, and spiritual response editorially.

Avoid giving away every argument or turning the piece into a full sermon recap.
Use specific enough details to create interest, but keep directing the reader
back to the original video. Target ~{word_count} words.

Return ONLY a JSON object with fields:
title, deck, article_body, primary_scripture, seo_title, meta_description, tags (string array)
"""


def generate_article(
    mode: str,
    pastor_name: str,
    church_or_ministry: str,
    sermon_title: str,
    video_url: str,
    published_date: str,
    transcript_quality: str,
    primary_scripture: str,
    strong_quotes: List[str],
    summary: str,
    keywords: List[str],
    main_theme: str,
    word_count: int = 500,
    community_comments: Optional[List[Dict[str, object]]] = None,
) -> Dict:
    """
    Generate a news article, devotional blog, or Before You Watch guide with GPT.
    Returns dict with article fields + token usage/cost.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set")

    client = OpenAI(api_key=OPENAI_API_KEY)

    system_templates = {
        "news": _NEWS_SYSTEM,
        "blog": _BLOG_SYSTEM,
        "before_watch": _BEFORE_WATCH_SYSTEM,
    }
    system_template = system_templates.get(mode, _BLOG_SYSTEM)
    system_prompt = system_template.format(word_count=word_count)

    comment_context = "No YouTube comment context available."
    if community_comments:
        safe_comments = [
            {
                "text": str(item.get("text", ""))[:500],
                "like_count": item.get("like_count", 0),
            }
            for item in community_comments[:5]
        ]
        comment_context = json.dumps(safe_comments, indent=2, ensure_ascii=False)

    user_content = f"""
Pastor: {pastor_name}
Church/Ministry: {church_or_ministry}
Sermon Title: {sermon_title}
Published Date: {published_date}
Primary Scripture: {primary_scripture}
Main Theme: {main_theme}
Sermon Summary:
{summary}

Strong Quotes:
{json.dumps(strong_quotes, indent=2)}

Keywords: {', '.join(keywords)}
Video URL: {video_url}
Transcript Quality: {transcript_quality}
Content Mode: {mode}

YouTube Comment Context for Community Response:
{comment_context}

Write the article now.
""".strip()

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_object"},
        temperature=0.7,
        max_tokens=2500,
    )

    usage = response.usage
    input_tokens = usage.prompt_tokens
    output_tokens = usage.completion_tokens
    cost = (input_tokens / 1_000_000 * _INPUT_COST_PER_1M) + (
        output_tokens / 1_000_000 * _OUTPUT_COST_PER_1M
    )

    try:
        data = json.loads(response.choices[0].message.content)
    except (json.JSONDecodeError, AttributeError):
        fallback_title = f"{pastor_name}: {sermon_title}"
        if mode == "before_watch":
            fallback_title = f"Why This Sermon Is Worth Watching: {pastor_name} on {main_theme or sermon_title}"
        data = {
            "title": fallback_title,
            "deck": main_theme,
            "article_body": response.choices[0].message.content or "",
            "primary_scripture": primary_scripture,
            "seo_title": f"{pastor_name} sermon — {sermon_title}",
            "meta_description": main_theme,
            "tags": keywords[:5],
        }

    data.update({"input_tokens": input_tokens, "output_tokens": output_tokens, "cost": cost})
    return data
