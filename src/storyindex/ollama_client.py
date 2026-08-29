"""Thin client for a local Ollama server. No other model backend is used —
all tagging must stay on-machine, so this is the one place network calls
happen and it only ever talks to localhost."""

from __future__ import annotations

import json

import requests

DEFAULT_HOST = "http://localhost:11434"


class OllamaError(RuntimeError):
    pass


def generate_json(
    prompt: str,
    model: str,
    host: str = DEFAULT_HOST,
    temperature: float = 0.2,
    timeout: int = 120,
) -> dict:
    """Send prompt to the local model, requesting strict JSON output.
    Raises OllamaError on transport failure or unparsable output."""
    try:
        resp = requests.post(
            f"{host}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "format": "json",
                "stream": False,
                "options": {"temperature": temperature},
            },
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise OllamaError(f"request to local Ollama server failed: {exc}") from exc

    raw = resp.json().get("response", "")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OllamaError(f"model did not return valid JSON: {raw!r}") from exc


def embed(
    text: str,
    model: str,
    host: str = DEFAULT_HOST,
    timeout: int = 60,
) -> list[float]:
    """Return an embedding vector for text from a local Ollama embedding
    model (e.g. nomic-embed-text). Same localhost-only constraint as
    generate_json."""
    try:
        resp = requests.post(
            f"{host}/api/embeddings",
            json={"model": model, "prompt": text},
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise OllamaError(f"embedding request to local Ollama server failed: {exc}") from exc

    vec = resp.json().get("embedding")
    if not isinstance(vec, list) or not vec:
        raise OllamaError(f"model did not return an embedding: {resp.text!r}")
    return vec
