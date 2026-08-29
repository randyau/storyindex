"""Thin client for a local Ollama server. No other model backend is used —
all tagging must stay on-machine, so this is the one place network calls
happen and it only ever talks to localhost."""

from __future__ import annotations

import json

import requests

DEFAULT_HOST = "http://localhost:11434"

# Ollama defaults num_ctx to a small window (historically 2048-4096)
# regardless of what the model itself supports, silently truncating any
# prompt longer than that instead of erroring — so a story tagging prompt
# that doesn't set this gets the model reading only the first page or two
# of a story it's supposed to tag in full. We size it per-request from the
# actual prompt length instead, rounded up to the next power-of-two-ish
# bucket so KV-cache allocation stays predictable rather than a fresh size
# every call. Capped at 32768: bigger buckets exist for the handful of
# very long stories, but past this point VRAM on modest cards runs out
# before context does, and a story that long was going to lose fidelity to
# an LLM's attention anyway.
_CTX_BUCKETS = [4096, 8192, 16384, 32768]
CHARS_PER_TOKEN = 4  # rough estimator; erring high costs a bit of unused KV-cache, erring low truncates input
# Public so callers building a prompt (classify.build_prompt) can pre-
# truncate oversized input to fit, rather than silently exceeding this and
# letting Ollama truncate the raw token stream instead - which cuts from
# whichever end produces the newest tokens, and for a prompt that's
# instructions-then-content, that's the content's tail, not a clean
# no-op. For a prompt that's content-then-instructions, or where the
# content is what's oversized, the caller needs this number to truncate
# in a way that keeps the instructions intact.
MAX_CTX_TOKENS = _CTX_BUCKETS[-1]


def _estimate_num_ctx(prompt: str) -> int:
    est_tokens = len(prompt) // CHARS_PER_TOKEN
    # Leave headroom for the model's own output tokens on top of the prompt.
    needed = est_tokens + 512
    for bucket in _CTX_BUCKETS:
        if needed <= bucket:
            return bucket
    return _CTX_BUCKETS[-1]


class OllamaError(RuntimeError):
    pass


def generate_json(
    prompt: str,
    model: str,
    host: str = DEFAULT_HOST,
    temperature: float = 0.2,
    timeout: int = 300,
) -> dict:
    """Send prompt to the local model, requesting strict JSON output.
    Raises OllamaError on transport failure or unparsable output. Default
    timeout is generous because num_ctx now scales with prompt length (see
    _estimate_num_ctx) — a long story means a long prefill, not just a
    long generation."""
    try:
        resp = requests.post(
            f"{host}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "format": "json",
                "stream": False,
                "options": {"temperature": temperature, "num_ctx": _estimate_num_ctx(prompt)},
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
