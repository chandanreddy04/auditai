"""
Single seam between the app and whichever LLM backend is running -
Ollama locally by default, Groq's hosted free tier when GROQ_API_KEY is
set. Same dual-backend pattern proven out in InvoiceIQ: every caller
only ever calls chat() here, validates the JSON it gets back against
its own Pydantic schema, and raises LLMUnavailableError on failure.
Swapping backends changes where the words come from, not that contract.

chat_with_image() is the vision counterpart to chat() - used only for
scanned PDFs (no text layer) and directly-uploaded photos/screenshots.
Same dual-backend split, same LLMUnavailableError contract - the caller
never needs to know or care which backend actually answered.
"""

import json
import logging

from app.core.config import (
    GROQ_API_KEY, GROQ_MODEL, GROQ_VISION_MODEL, OLLAMA_MODEL, OLLAMA_VISION_MODEL,
)

logger = logging.getLogger(__name__)

MODEL_NAME = GROQ_MODEL if GROQ_API_KEY else OLLAMA_MODEL


class LLMUnavailableError(Exception):
    """Raised when the active LLM backend can't be reached or returns unusable output."""


def chat(messages: list[dict], schema: dict | None = None, temperature: float = 0.0) -> str:
    if GROQ_API_KEY:
        return _chat_groq(messages, schema, temperature)
    return _chat_ollama(messages, schema, temperature)


def _chat_ollama(messages: list[dict], schema: dict | None, temperature: float) -> str:
    import ollama

    try:
        response = ollama.chat(
            model=OLLAMA_MODEL,
            messages=messages,
            format=schema,
            options={"temperature": temperature},
        )
        return response["message"]["content"]
    except Exception as e:
        raise LLMUnavailableError(str(e)) from e


def _chat_groq(messages: list[dict], schema: dict | None, temperature: float) -> str:
    import httpx

    messages = [dict(m) for m in messages]
    extra: dict = {}
    if schema is not None:
        instruction = f"Respond with ONLY a JSON object matching this schema: {json.dumps(schema)}"
        if messages and messages[0]["role"] == "system":
            messages[0]["content"] += "\n\n" + instruction
        else:
            messages.insert(0, {"role": "system", "content": instruction})
        extra["response_format"] = {"type": "json_object"}

    try:
        resp = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={"model": GROQ_MODEL, "messages": messages, "temperature": temperature, **extra},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        body = getattr(getattr(e, "response", None), "text", "")
        logger.warning("Groq call failed (model=%s): %s %s", GROQ_MODEL, e, body[:300])
        raise LLMUnavailableError(str(e)) from e


def chat_with_image(text: str, image_bytes: bytes, schema: dict | None = None, temperature: float = 0.0) -> str:
    if GROQ_API_KEY:
        return _chat_vision_groq(text, image_bytes, schema, temperature)
    return _chat_vision_ollama(text, image_bytes, schema, temperature)


def _chat_vision_ollama(text: str, image_bytes: bytes, schema: dict | None, temperature: float) -> str:
    import ollama

    try:
        response = ollama.chat(
            model=OLLAMA_VISION_MODEL,
            messages=[{"role": "user", "content": text, "images": [image_bytes]}],
            format=schema,
            options={"temperature": temperature},
        )
        return response["message"]["content"]
    except Exception as e:
        raise LLMUnavailableError(str(e)) from e


def _chat_vision_groq(text: str, image_bytes: bytes, schema: dict | None, temperature: float) -> str:
    import base64

    import httpx

    if schema is not None:
        text += f"\n\nRespond with ONLY a JSON object matching this schema: {json.dumps(schema)}"
    b64_image = base64.b64encode(image_bytes).decode("ascii")
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_image}"}},
        ],
    }]
    extra = {"response_format": {"type": "json_object"}} if schema is not None else {}

    try:
        resp = httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={"model": GROQ_VISION_MODEL, "messages": messages, "temperature": temperature, **extra},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        body = getattr(getattr(e, "response", None), "text", "")
        logger.warning("Groq vision call failed (model=%s): %s %s", GROQ_VISION_MODEL, e, body[:300])
        raise LLMUnavailableError(str(e)) from e


def is_available() -> bool:
    if GROQ_API_KEY:
        try:
            _chat_groq([{"role": "user", "content": "ping"}], schema=None, temperature=0)
            return True
        except LLMUnavailableError:
            return False
    try:
        import ollama
        ollama.list()
        return True
    except Exception:
        return False
