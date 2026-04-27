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

_SYSTEM = """\
You are a Christian journalism editor reviewing an AI-generated article for accuracy and risk.

Review the article against the original sermon summary and quotes. Check for:
1. Claims not supported by the source
2. Exaggeration of the pastor's statements
3. Strong political or social controversy language
4. Distortion of the sermon's core message
5. Incorrect or unsupported scripture connections
6. Sentences too similar to the transcript (plagiarism risk)

Be thorough but fair. Return ONLY a valid JSON object:
{
  "risk_level": "LOW" | "MEDIUM" | "HIGH",
  "status": "PASS" | "REVIEW" | "FAIL",
  "reviewer_notes": ["note if any"],
  "unsupported_claims": ["claim if any"],
  "quote_accuracy": "PASS" | "REVIEW" | "FAIL",
  "scripture_accuracy": "PASS" | "REVIEW" | "FAIL"
}
"""


def review_article(
    transcript_summary: str,
    strong_quotes: List[str],
    article: str,
    primary_scripture: str,
    pastor_name: str,
) -> Dict:
    """
    Run risk review on the generated article.
    Returns risk assessment dict + token usage/cost.
    """
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set")

    client = OpenAI(api_key=OPENAI_API_KEY)

    user_content = f"""
Pastor: {pastor_name}
Primary Scripture: {primary_scripture}

Original Transcript Summary:
{transcript_summary[:3000]}

Verified Quotes from Transcript:
{json.dumps(strong_quotes, indent=2)}

Generated Article (first 3000 chars):
{article[:3000]}

Review the article now.
""".strip()

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
        max_tokens=600,
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
            "risk_level": "MEDIUM",
            "status": "REVIEW",
            "reviewer_notes": ["Could not parse risk review response"],
            "unsupported_claims": [],
            "quote_accuracy": "REVIEW",
            "scripture_accuracy": "REVIEW",
        }

    data.update({"input_tokens": input_tokens, "output_tokens": output_tokens, "cost": cost})
    return data
