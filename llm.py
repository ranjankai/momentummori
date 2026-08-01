"""
Gemini client: strict-JSON generation with a model waterfall.

WHY A WATERFALL
---------------
A single model is a single point of failure. Google returns 429 under
quota pressure and 503 during capacity events, and a strategy run that
dies because one tier is busy is worse than one that quietly steps down
to a cheaper model. Order is best-first:

    gemini-3.6-flash  ->  gemini-3.5-flash  ->  gemini-3.5-flash-lite

All three were verified live on 01-Aug-2026 returning valid output
against a strict responseSchema with extended thinking enabled. Note
Flash-Lite has thinking OFF by default and only reasons when
`thinkingLevel` is set explicitly -- which this module always does.

FAILURE POLICY
--------------
Nothing in here raises. `generate_json` returns None when every tier has
been exhausted, and the caller decides what a missing answer means. An
LLM outage must never take down a strategy run.

DETERMINISM
-----------
Responses are cached on disk keyed by a hash of (prompt + schema), NOT by
model. A repeated question therefore returns byte-identical output and
costs nothing -- which is what makes a re-run of a backtest reproducible
even though the underlying call is not.
"""

import hashlib
import json
import logging
import os
import time

import requests

import config

logger = logging.getLogger("momentum_tracker.llm")

# HTTP statuses worth retrying the SAME model for. Everything else (400
# malformed request, 401/403 bad key, 404 unknown model) is permanent for
# that tier, so we drop straight to the next one.
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class LLMUnavailable(Exception):
    """Internal signal that a tier is exhausted. Never escapes this module."""


def _cache_key(prompt: str, schema: dict) -> str:
    blob = json.dumps({"p": prompt, "s": schema}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _cache_path(key: str) -> str:
    os.makedirs(config.LLM_CACHE_DIR, exist_ok=True)
    return os.path.join(config.LLM_CACHE_DIR, f"{key}.json")


def _cache_read(key: str):
    if not config.LLM_CACHE_ENABLED:
        return None
    path = _cache_path(key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        logger.debug("LLM cache hit %s (model=%s)", key, payload.get("_model"))
        return payload
    except (OSError, ValueError) as exc:
        logger.warning("Unreadable LLM cache entry %s: %s", path, exc)
        return None


def _cache_write(key: str, payload: dict) -> None:
    if not config.LLM_CACHE_ENABLED:
        return
    try:
        with open(_cache_path(key), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
    except OSError as exc:
        # A read-only or full disk must not break the run.
        logger.warning("Could not write LLM cache %s: %s", key, exc)


def _call_model(model: str, prompt: str, schema: dict) -> dict:
    """One model, with retries. Raises LLMUnavailable when exhausted."""
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "thinkingConfig": {"thinkingLevel": config.LLM_THINKING_LEVEL},
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }
    url = config.LLM_ENDPOINT.format(model=model)
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": config.GEMINI_API_KEY,
    }

    for attempt in range(1, config.LLM_MAX_RETRIES + 1):
        try:
            resp = requests.post(
                url, json=body, headers=headers,
                timeout=config.LLM_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            logger.warning("%s attempt %d/%d: transport error: %s",
                           model, attempt, config.LLM_MAX_RETRIES, exc)
            _sleep(attempt)
            continue

        if resp.status_code == 200:
            try:
                data = resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                parsed = json.loads(text)
            except (ValueError, KeyError, IndexError) as exc:
                # 200 but unusable: a truncated candidate or a safety block.
                logger.warning("%s attempt %d/%d: unparseable 200 response: %s",
                               model, attempt, config.LLM_MAX_RETRIES, exc)
                _sleep(attempt)
                continue
            usage = data.get("usageMetadata", {})
            logger.info("%s OK (total_tokens=%s, thoughts=%s)", model,
                        usage.get("totalTokenCount"),
                        usage.get("thoughtsTokenCount"))
            parsed["_model"] = model
            parsed["_usage"] = {
                "total_tokens": usage.get("totalTokenCount"),
                "thought_tokens": usage.get("thoughtsTokenCount"),
            }
            return parsed

        detail = resp.text[:250].replace("\n", " ")
        if resp.status_code in _RETRYABLE_STATUS:
            logger.warning("%s attempt %d/%d: HTTP %d (retryable): %s",
                           model, attempt, config.LLM_MAX_RETRIES,
                           resp.status_code, detail)
            _sleep(attempt)
            continue

        # Permanent for this tier -- do not burn retries on it.
        logger.error("%s: HTTP %d (permanent, dropping tier): %s",
                     model, resp.status_code, detail)
        raise LLMUnavailable(f"{model} HTTP {resp.status_code}")

    raise LLMUnavailable(f"{model} exhausted after {config.LLM_MAX_RETRIES} attempts")


def _sleep(attempt: int) -> None:
    """Exponential backoff: 2s, 4s, 8s -- same shape as the NSE client."""
    time.sleep(config.LLM_RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))


def generate_json(prompt: str, schema: dict, models=None):
    """
    Ask Gemini for JSON matching `schema`. Returns the parsed dict on
    success (with `_model` and `_usage` attached), or None if disabled,
    unconfigured, or every tier failed. Never raises.
    """
    if not config.LLM_ENABLED:
        logger.info("LLM disabled in config; returning None")
        return None
    if not config.GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY is not set (check .env); returning None")
        return None

    key = _cache_key(prompt, schema)
    cached = _cache_read(key)
    if cached is not None:
        return cached

    for model in (models or config.LLM_MODEL_WATERFALL):
        try:
            result = _call_model(model, prompt, schema)
        except LLMUnavailable as exc:
            logger.warning("Falling through: %s", exc)
            continue
        _cache_write(key, result)
        return result

    logger.error("Every model tier failed for this request; returning None")
    return None
