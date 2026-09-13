"""
shared/llm.py

Thin, optional wrapper around the Groq API for severity assessment.

Design choice: only Medical and Factory incidents are driven primarily by
free-text bystander descriptions ("person unconscious", "chemical spill"),
so those are the two modules where an LLM call adds real reasoning value.
Fire severity is computed from structured inputs (spread risk, wind,
crowd proximity, etc.) and Traffic severity from live road/traffic data —
both are already deterministic multi-factor scores, so wiring an LLM call
into them would be reasoning-for-show rather than reasoning that changes
the outcome. This module is intentionally only used by modules/medical and
modules/factory.

Provider: Groq (https://console.groq.com), which offers a generous free
tier and an OpenAI-compatible REST API, so no paid API key is required.
Model used: "llama-3.1-8b-instant" (fast + free-tier friendly). Swap the
GROQ_MODEL env var if you want a bigger model (e.g. "llama-3.3-70b-versatile").

Usage is fully optional and fails safe: if USE_LLM_SEVERITY is not set to
"1", or the API key is missing, or the call errors out for any reason,
callers should catch the raised RuntimeError and fall back to their
rule-based assessment. Nothing about the system depends on this file
being usable.
"""

import json
import os
import urllib.request
import urllib.error

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-20b"

def _groq_chat(prompt: str, max_tokens: int) -> str:
    """
    Minimal Groq chat-completions call using only the stdlib (no extra
    dependency needed beyond `requests` if you'd rather use that — this
    avoids adding a new package to requirements.txt).
    """
    if os.environ.get("USE_LLM_SEVERITY") != "1":
        raise RuntimeError("LLM severity assessment is disabled (set USE_LLM_SEVERITY=1 to enable).")

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not set.")

    model = os.environ.get("GROQ_MODEL", DEFAULT_MODEL)

    payload = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        GROQ_API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "smart-city-incident-response/1.0",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Groq API HTTP error {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Groq API network error: {e}") from e

    try:
        return body["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"Unexpected Groq API response shape: {body}") from e


def _strip_code_fence(text: str) -> str:
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    return text


def llm_assess(description: str, domain_hint: str) -> tuple[int, str]:
    """
    Ask an LLM to assess incident severity from a free-text description.

    Returns (score 1-5, one-sentence reasoning).
    Raises RuntimeError if the LLM path is disabled, unavailable, or fails
    for any reason -- callers are expected to catch this and fall back to
    rule-based assessment.
    """
    prompt = (
        f"You are an emergency dispatch severity assessor for a {domain_hint} incident.\n"
        f'Bystander report: "{description}"\n\n'
        "Assess severity on a 1-5 scale (1 = minor, 5 = critical/life-threatening).\n"
        "Respond with ONLY a JSON object, no other text, in exactly this form:\n"
        '{"score": <integer 1-5>, "reasoning": "<one concise sentence>"}'
    )

    text = _strip_code_fence(_groq_chat(prompt, max_tokens=150))
    data = json.loads(text)
    score = int(data["score"])
    reasoning = str(data["reasoning"])
    score = max(1, min(5, score))
    return score, reasoning


VALID_TYPES = ["fire", "medical", "factory", "road_blockage", "other"]


def llm_classify_type(description: str) -> str:
    """
    Ask an LLM which module should handle a bystander report when the
    reporter selected "Not sure / Other" instead of a specific category.
    This is the Orchestrator's dynamic-action-selection step in action:
    the agent, not the human, decides which module owns the incident.

    Returns one of VALID_TYPES. Raises RuntimeError on any failure --
    callers should catch this and fall back to a simple keyword guess.
    """
    prompt = (
        "A bystander reported a city incident but did not pick a category. "
        f'Report: "{description}"\n\n'
        f"Classify it into exactly one of: {', '.join(VALID_TYPES)}.\n"
        'Respond with ONLY a JSON object: {"type": "<one of the categories above>"}'
    )

    text = _strip_code_fence(_groq_chat(prompt, max_tokens=300))
    data = json.loads(text)
    incident_type = str(data["type"]).strip().lower()
    if incident_type not in VALID_TYPES:
        raise RuntimeError(f"LLM returned an unrecognized type: {incident_type!r}")
    return incident_type
