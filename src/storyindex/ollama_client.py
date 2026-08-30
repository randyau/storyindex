"""Thin client for a local Ollama server. No other model backend is used —
all tagging must stay on-machine, so this is the one place network calls
happen and it only ever talks to localhost."""

from __future__ import annotations

import json
from functools import lru_cache

import requests

DEFAULT_HOST = "http://localhost:11434"

# Ollama defaults num_ctx to a small window (historically 2048-4096)
# regardless of what the model itself supports, silently truncating any
# prompt longer than that instead of erroring — so a story tagging prompt
# that doesn't set this gets the model reading only the first page or two
# of a story it's supposed to tag in full. We size it per-request from the
# actual prompt length instead, rounded up to the next power-of-two-ish
# bucket so KV-cache allocation stays predictable rather than a fresh size
# every call. Buckets go up to 131072 for hardware/models that can actually
# use that much; MAX_CTX_TOKENS below is just the *default* ceiling
# (see classify._effective_max_ctx_tokens for where a bigger bucket
# actually gets requested, driven by settings.max_ctx_tokens).
_CTX_BUCKETS = [4096, 8192, 16384, 32768, 65536, 131072]
# The largest bucket this client will ever request, regardless of what a
# caller's own (settings-derived) ceiling asks for - a hard backstop, not
# a recommendation. See MAX_CTX_TOKENS below for the actual default.
ABSOLUTE_MAX_CTX_TOKENS = _CTX_BUCKETS[-1]
CHARS_PER_TOKEN = 4  # rough estimator; erring high costs a bit of unused KV-cache, erring low truncates input
# Default/safe ceiling if the user hasn't told /settings about bigger
# hardware: fits comfortably on a modest (~8GB) card, and a story that
# long was going to lose fidelity to an LLM's attention anyway. Public so
# callers building a prompt (classify.build_prompt) can pre-truncate
# oversized input to fit, rather than silently exceeding this and letting
# Ollama truncate the raw token stream instead - which cuts from whichever
# end produces the newest tokens, and for a prompt that's
# instructions-then-content, that's the content's tail, not a clean no-op.
MAX_CTX_TOKENS = 32768
MIN_CTX_TOKENS = _CTX_BUCKETS[0]  # sane floor - never worth requesting less


def _estimate_num_ctx(prompt: str, max_ctx: int = MAX_CTX_TOKENS) -> int:
    est_tokens = len(prompt) // CHARS_PER_TOKEN
    # Leave headroom for the model's own output tokens on top of the prompt.
    needed = est_tokens + 512
    usable = [b for b in _CTX_BUCKETS if b <= max_ctx] or [max_ctx]
    for bucket in usable:
        if needed <= bucket:
            return bucket
    return usable[-1]


class OllamaError(RuntimeError):
    pass


def generate_json(
    prompt: str,
    model: str,
    host: str = DEFAULT_HOST,
    temperature: float = 0.2,
    timeout: int = 300,
    max_ctx: int = MAX_CTX_TOKENS,
) -> dict:
    """Send prompt to the local model, requesting strict JSON output.
    Raises OllamaError on transport failure or unparsable output. Default
    timeout is generous because num_ctx now scales with prompt length (see
    _estimate_num_ctx) — a long story means a long prefill, not just a
    long generation. max_ctx caps which bucket _estimate_num_ctx can pick -
    callers with more headroom (see model_max_context / settings) can raise
    it to fit more of a long story in one call."""
    try:
        resp = requests.post(
            f"{host}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "format": "json",
                "stream": False,
                "options": {"temperature": temperature, "num_ctx": _estimate_num_ctx(prompt, max_ctx)},
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


@lru_cache(maxsize=32)
def model_max_context(model: str, host: str = DEFAULT_HOST, timeout: float = 10.0) -> int | None:
    """The context length this specific model was actually built/quantized
    for, straight from Ollama's own model metadata (`/api/show`) - so a
    long story's chunk budget (see classify._effective_max_ctx_tokens)
    isn't capped by our own bucket ceiling alone when the model itself
    supports less. Best-effort: returns None on any failure (server
    unreachable, unexpected response shape) rather than raising, since a
    missing value just means the caller falls back to its hardware-only
    ceiling. Cached per (model, host) for the life of the process - the
    same installed model's context length doesn't change mid-run, and this
    can get called once per story in a large extraction batch."""
    try:
        resp = requests.post(f"{host}/api/show", json={"model": model}, timeout=timeout)
        resp.raise_for_status()
        info = resp.json().get("model_info") or {}
    except (requests.RequestException, ValueError):
        return None
    for key, value in info.items():
        if key.endswith(".context_length") and isinstance(value, int) and value > 0:
            return value
    return None


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
#
# The "instruct" entries below (7b default, 14b/32b step-ups) reflect a real
# side-by-side pilot on ~8GB VRAM: qwen2.5:7b-instruct's tag output was
# checked against a manual read of sample stories and matched closely, while
# two smaller/differently-tuned ~4-7B candidates tried in the same pilot
# either hallucinated details not in the text or missed a story's central
# theme outright. Neither refused to classify mature content — that's not a
# meaningful axis for picking among these three. The 14b/32b step-ups are
# the same model family scaled up for more VRAM, not independently
# benchmarked yet.
RECOMMENDED_MODELS = [
    {
        "name": "qwen2.5:7b-instruct",
        "purpose": "extraction (default)",
        "why": "128k context window; in a side-by-side pilot against a manual "
               "read of sample stories, its tags matched most closely of the "
               "candidates tried. Fits comfortably on an 8GB-VRAM GPU at "
               "~8-20s/story once warm.",
    },
    {
        "name": "qwen2.5:14b-instruct",
        "purpose": "extraction (higher quality)",
        "why": "same family as the default, scaled up - try this first if 7b's "
               "tagging feels shallow and you have ~16GB VRAM to spare.",
    },
    {
        "name": "qwen2.5:32b-instruct",
        "purpose": "extraction (highest quality)",
        "why": "same family again, for ~24GB+ VRAM setups wanting the best "
               "judgment on nuanced or long stories.",
    },
    {
        "name": "nomic-embed-text",
        "purpose": "clustering / embeddings (required)",
        "why": "the embedding model the normalization pass uses to cluster "
               "tag_candidates - install this regardless of which extraction "
               "model you pick.",
    },
]
