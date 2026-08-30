"""Extraction pass: propose free-form tag candidates for a single story via
a local model. This is pass 1 of 2 — normalization/clustering into the
canonical tag vocabulary happens separately, after candidates accumulate.

Prompts are versioned files under prompts/, never inline strings, so a
prompt change is a diffable, re-runnable artifact rather than a silent
behavior change.
"""

from __future__ import annotations

from pathlib import Path

from storyindex import ollama_client
from storyindex.ollama_client import (
    CHARS_PER_TOKEN,
    DEFAULT_HOST,
    MAX_CTX_TOKENS,
    OllamaError,
    generate_json,
)
from storyindex.signature import StorySignature

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"

# A story longer than this would need more context than Ollama's largest
# num_ctx bucket (see ollama_client) gives the request. Left alone, Ollama
# truncates the raw token stream to fit - which cuts from the tail, and
# every prompt template here puts the story *after* the instructions, so
# that truncation would silently eat the instructions and leave the model
# staring at a fragment of the story with no idea what to do with it (this
# is what a "response missing 'tags' list: {}" failure on a very long
# story turned out to be). Truncating body_text ourselves, up front,
# guarantees the instructions always survive intact even if the story
# itself has to lose its ending.
#
# extract_tags() no longer relies on this truncation for a genuinely long
# story - see _chunk_body_text below - but build_prompt keeps this as its
# default cap so any other caller building a one-shot prompt still gets
# the same safety net it always has.
_PROMPT_OVERHEAD_CHARS = 4000  # rough budget for instructions + title/author, in characters


def _max_body_chars(max_ctx_tokens: int) -> int:
    return (max_ctx_tokens - 512) * CHARS_PER_TOKEN - _PROMPT_OVERHEAD_CHARS


MAX_BODY_CHARS = _max_body_chars(MAX_CTX_TOKENS)

# 0, not 1: a prompt can legitimately scope itself to a facet that's absent
# from a given story (e.g. a setting/clothing/ethnicity pass on a story
# that specifies none of those) - forcing a minimum of one tag would
# pressure the model into padding with something that doesn't belong
# rather than truthfully returning nothing for that facet.
MIN_TAGS = 0
MAX_TAGS = 20


class ExtractionError(RuntimeError):
    pass


def load_prompt_template(prompt_version: str) -> str:
    path = PROMPTS_DIR / f"extract_{prompt_version}.md"
    if not path.exists():
        raise ExtractionError(f"no prompt file for version {prompt_version!r}: {path}")
    return path.read_text(encoding="utf-8")


def _render_prompt(template: str, title: str, author: str, body_text: str) -> str:
    # Plain placeholder substitution, not str.format() — the prompt itself
    # contains literal JSON braces (the output-shape example) that .format
    # would misparse as format fields.
    return (
        template
        .replace("{title}", title or "(untitled)")
        .replace("{author}", author or "(unknown)")
        .replace("{body_text}", body_text)
    )


def build_prompt(template: str, sig: StorySignature, max_body_chars: int = MAX_BODY_CHARS) -> str:
    body_text = sig.body_text
    if len(body_text) > max_body_chars:
        body_text = body_text[:max_body_chars] + "\n\n[story truncated for length]"
    return _render_prompt(template, sig.title, sig.author, body_text)


def _normalize_tags(raw_tags: list) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for tag in raw_tags:
        if not isinstance(tag, str):
            continue
        t = tag.strip().lower()
        # A prompt that groups its example tags under category headers (e.g.
        # "category: example") can get echoed back verbatim by a model,
        # especially on long inputs where instructions further up the
        # prompt lose their grip. No prompt asks for a colon inside a tag,
        # so take whatever follows the last one as the intended tag rather
        # than dropping the whole thing.
        if ":" in t:
            t = t.rsplit(":", 1)[1].strip()
        if not t or t in seen:
            continue
        seen.add(t)
        cleaned.append(t)
    return cleaned


def _chunk_body_text(body_text: str, max_chars: int) -> list[str]:
    """Split body_text into as few pieces as possible, each within
    max_chars, breaking only at paragraph boundaries (body_text's
    paragraphs are always \\n\\n-separated - see StorySignature's contract)
    so a chunk boundary never lands mid-sentence. Greedy packing: keep
    adding whole paragraphs to the current chunk until the next one
    wouldn't fit, then start a new chunk - this is what keeps the chunk
    count minimal for a given max_chars, rather than e.g. splitting every
    story into equal halves regardless of how much headroom max_chars
    actually has. A single paragraph longer than max_chars on its own
    (rare - a story with no paragraph breaks at all) is hard-sliced as a
    last resort so this never returns a chunk that's still oversized."""
    if len(body_text) <= max_chars:
        return [body_text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in body_text.split("\n\n"):
        joined_len = len(paragraph) + (2 if current else 0)  # + "\n\n" join
        if current and current_len + joined_len > max_chars:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
            joined_len = len(paragraph)
        if len(paragraph) > max_chars:
            if current:
                chunks.append("\n\n".join(current))
                current = []
                current_len = 0
            chunks.extend(paragraph[i : i + max_chars] for i in range(0, len(paragraph), max_chars))
            continue
        current.append(paragraph)
        current_len += joined_len
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _effective_max_ctx_tokens(model: str, host: str, hardware_ctx_cap: int) -> int:
    """The largest safe num_ctx for this (model, host) pair, so a long
    story needs as few chunks as possible: capped by hardware_ctx_cap (the
    user's own /settings VRAM budget - actual GPU memory isn't something
    this process can reliably auto-detect, especially since ollama_host
    could in principle point anywhere) and by whatever context length the
    model itself was actually built for (queried from Ollama; best-effort -
    if that lookup fails, the hardware cap alone is used)."""
    ceiling = min(hardware_ctx_cap, ollama_client.ABSOLUTE_MAX_CTX_TOKENS)
    model_max = ollama_client.model_max_context(model, host=host)
    if model_max:
        ceiling = min(ceiling, model_max)
    return max(ceiling, ollama_client.MIN_CTX_TOKENS)


def _extract_tags_once(
    title: str, author: str, body_text: str, model: str, prompt_text: str,
    host: str, max_ctx_tokens: int, story_id: str,
) -> list[str]:
    prompt = _render_prompt(prompt_text, title, author, body_text)
    try:
        result = generate_json(prompt, model=model, host=host, max_ctx=max_ctx_tokens)
    except OllamaError as exc:
        raise ExtractionError(f"story {story_id}: {exc}") from exc

    raw_tags = result.get("tags")
    if not isinstance(raw_tags, list):
        raise ExtractionError(f"story {story_id}: response missing 'tags' list: {result!r}")

    tags = _normalize_tags(raw_tags)
    if not (MIN_TAGS <= len(tags) <= MAX_TAGS):
        raise ExtractionError(
            f"story {story_id}: got {len(tags)} tags after cleaning, expected "
            f"{MIN_TAGS}-{MAX_TAGS}: {tags!r}"
        )
    return tags


def extract_tags(
    sig: StorySignature,
    model: str,
    prompt_text: str,
    host: str = DEFAULT_HOST,
    max_ctx_tokens: int = MAX_CTX_TOKENS,
) -> list[str]:
    """Run the extraction pass for one story against a given prompt
    template's text. Returns a deduped, cleaned list of candidate tag
    strings. Raises ExtractionError on any failure — caller decides
    whether to skip and continue or abort the batch.

    prompt_text is the caller's responsibility to source (the prompt
    library in the DB, or load_prompt_template() for the legacy
    file-based versions) — this function has no opinion on storage.

    max_ctx_tokens is the caller's hardware ceiling (settings.py's
    max_ctx_tokens, default matches ollama_client.MAX_CTX_TOKENS). Most
    stories fit in one call under that ceiling alone and nothing else
    happens. For a story that doesn't, this queries the model's own real
    context length (best-effort, network round trip - only paid when
    actually needed) to see if a bigger bucket than the hardware ceiling
    alone would suggest is actually usable, then splits the story into as
    few paragraph-aligned chunks as fit that ceiling, tags each chunk
    separately, and returns the union of every chunk's tags (each chunk is
    told it's "part N of M of a longer story" so the model doesn't read a
    fragment as if it were the whole thing)."""
    hardware_max_chars = _max_body_chars(max_ctx_tokens)
    if len(sig.body_text) <= hardware_max_chars:
        return _extract_tags_once(
            sig.title, sig.author, sig.body_text, model, prompt_text, host, max_ctx_tokens, sig.id
        )

    effective_ctx = _effective_max_ctx_tokens(model, host, max_ctx_tokens)
    max_chars = _max_body_chars(effective_ctx)
    chunks = _chunk_body_text(sig.body_text, max_chars)
    if len(chunks) == 1:
        return _extract_tags_once(
            sig.title, sig.author, chunks[0], model, prompt_text, host, effective_ctx, sig.id
        )

    merged: list[str] = []
    seen: set[str] = set()
    for i, chunk in enumerate(chunks, start=1):
        note = f"[this is part {i} of {len(chunks)} of a longer story]\n\n"
        tags = _extract_tags_once(
            sig.title, sig.author, note + chunk, model, prompt_text, host, effective_ctx, sig.id
        )
        for t in tags:
            if t not in seen:
                seen.add(t)
                merged.append(t)
    return merged
