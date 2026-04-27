import os
import json
from typing import Dict, List

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
) -> Dict:
    """
    Generate a news article or devotional blog with GPT.
    Returns dict with article fields + token usage/cost.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set")

    client = OpenAI(api_key=OPENAI_API_KEY)

    system_template = _NEWS_SYSTEM if mode == "news" else _BLOG_SYSTEM
    system_prompt = system_template.format(word_count=word_count)

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
        data = {
            "title": f"{pastor_name}: {sermon_title}",
            "deck": main_theme,
            "article_body": response.choices[0].message.content or "",
            "primary_scripture": primary_scripture,
            "seo_title": f"{pastor_name} sermon — {sermon_title}",
            "meta_description": main_theme,
            "tags": keywords[:5],
        }

    data.update({"input_tokens": input_tokens, "output_tokens": output_tokens, "cost": cost})
    return data
