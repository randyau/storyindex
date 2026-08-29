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


def is_running(host: str = DEFAULT_HOST, timeout: float = 2.0) -> bool:
    try:
        requests.get(f"{host}/api/tags", timeout=timeout).raise_for_status()
        return True
    except requests.RequestException:
        return False


def list_models(host: str = DEFAULT_HOST, timeout: float = 5.0) -> list[dict]:
    """Locally-installed models (`ollama list` equivalent). Raises
    OllamaError if the server isn't reachable — check is_running() first
    if you want to distinguish "not running" from "running but errored"."""
    try:
        resp = requests.get(f"{host}/api/tags", timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise OllamaError(f"could not list models: {exc}") from exc
    return resp.json().get("models", [])


def start_server() -> None:
    """Launch `ollama serve` as a detached background process. If a server
    is already listening on the port, the new process just fails to bind
    and exits — harmless, so this is safe to call speculatively from a
    "start" button without checking is_running() first.
    Raises OllamaError if the `ollama` CLI isn't installed/on PATH, instead
    of letting FileNotFoundError bubble up as an unhandled 500."""
    import subprocess

    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise OllamaError(
            "the `ollama` CLI isn't installed or isn't on PATH — install it "
            "from https://ollama.com, or start it yourself and point "
            "/settings at the right host if it runs elsewhere"
        ) from exc


# Static guidance, not a live catalog — check `ollama pull <name>` works
# before relying on any of these still being current. Skewed toward large
# context windows since story bodies can run long and get truncated/
# degraded by a short context model well before hitting any token-count
# "limit" the extraction prompt itself imposes.
RECOMMENDED_MODELS = [
    {
        "name": "qwen2.5:14b-instruct",
        "purpose": "extraction (default)",
        "why": "128k context window, reliable JSON-mode output and instruction "
               "following; a solid default on a ~16GB-VRAM GPU.",
    },
    {
        "name": "qwen2.5:32b-instruct",
        "purpose": "extraction (higher quality)",
        "why": "same 128k-context family, noticeably better judgment on nuanced "
               "or long stories if you have ~24GB+ VRAM to run it.",
    },
    {
        "name": "llama3.1:8b-instruct",
        "purpose": "extraction (lighter)",
        "why": "128k context, much lighter footprint - a reasonable fallback on "
               "more limited hardware.",
    },
    {
        "name": "mistral-nemo:12b-instruct",
        "purpose": "extraction (alternative)",
        "why": "128k context, worth trying if Qwen's tagging style doesn't fit "
               "your taxonomy well.",
    },
    {
        "name": "nomic-embed-text",
        "purpose": "clustering / embeddings (required)",
        "why": "the embedding model the normalization pass uses to cluster "
               "tag_candidates - install this regardless of which extraction "
               "model you pick.",
    },
]
