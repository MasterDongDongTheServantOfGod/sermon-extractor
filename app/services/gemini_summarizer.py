import os
import json
from typing import Dict

from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite")

# Approximate pricing per 1M tokens
_INPUT_COST_PER_1M = 0.10
_OUTPUT_COST_PER_1M = 0.40

_PROMPT = """\
You are analyzing a sermon transcript to extract structured information.

Sermon Title: {title}
Pastor: {pastor}

Transcript (truncated to 15,000 chars):
{transcript}

Return ONLY a valid JSON object with these exact fields:
{{
  "summary": "2-3 paragraph summary of the sermon's main message and theological arguments",
  "primary_scripture": "Main Bible passage referenced (e.g. 'Philippians 3:8-11')",
  "strong_quotes": ["verbatim quote 1 from transcript", "verbatim quote 2", "verbatim quote 3"],
  "keywords": ["keyword1", "keyword2", "keyword3", "keyword4", "keyword5"],
  "main_theme": "One sentence describing the central theme",
  "sermon_type": "expository | topical | narrative | evangelistic"
}}
"""


def summarize_sermon(transcript: str, title: str, pastor_name: str) -> Dict:
    """
    Summarize sermon transcript with Gemini Flash Lite.
    Returns dict with summary, scripture, quotes, keywords + token usage/cost.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set")

    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = _PROMPT.format(
        title=title,
        pastor=pastor_name,
        transcript=transcript[:15_000],
    )

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )

    usage = response.usage_metadata
    input_tokens = getattr(usage, "prompt_token_count", 0) or 0
    output_tokens = getattr(usage, "candidates_token_count", 0) or 0
    cost = (input_tokens / 1_000_000 * _INPUT_COST_PER_1M) + (
        output_tokens / 1_000_000 * _OUTPUT_COST_PER_1M
    )

    raw = response.text.strip()
    # Strip markdown code fences if present
    for fence in ("```json", "```"):
        if raw.startswith(fence):
            raw = raw[len(fence):]
    if raw.endswith("```"):
        raw = raw[:-3]
    raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {
            "summary": raw,
            "primary_scripture": "",
            "strong_quotes": [],
            "keywords": [],
            "main_theme": "",
            "sermon_type": "expository",
        }

    data.update({"input_tokens": input_tokens, "output_tokens": output_tokens, "cost": cost})
    return data
