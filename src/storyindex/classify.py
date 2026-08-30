"""Extraction pass: propose free-form tag candidates for a single story via
a local model. This is pass 1 of 2 — normalization/clustering into the
canonical tag vocabulary happens separately, after candidates accumulate.

Prompts are versioned files under prompts/, never inline strings, so a
prompt change is a diffable, re-runnable artifact rather than a silent
behavior change.
"""

from __future__ import annotations

from pathlib import Path

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
_PROMPT_OVERHEAD_CHARS = 4000  # rough budget for instructions + title/author, in characters
MAX_BODY_CHARS = (MAX_CTX_TOKENS - 512) * CHARS_PER_TOKEN - _PROMPT_OVERHEAD_CHARS

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


def build_prompt(template: str, sig: StorySignature) -> str:
    body_text = sig.body_text
    if len(body_text) > MAX_BODY_CHARS:
        body_text = body_text[:MAX_BODY_CHARS] + "\n\n[story truncated for length]"
    # Plain placeholder substitution, not str.format() — the prompt itself
    # contains literal JSON braces (the output-shape example) that .format
    # would misparse as format fields.
    return (
        template
        .replace("{title}", sig.title or "(untitled)")
        .replace("{author}", sig.author or "(unknown)")
        .replace("{body_text}", body_text)
    )


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


def extract_tags(
    sig: StorySignature,
    model: str,
    prompt_text: str,
    host: str = DEFAULT_HOST,
) -> list[str]:
    """Run the extraction pass for one story against a given prompt
    template's text. Returns a deduped, cleaned list of candidate tag
    strings. Raises ExtractionError on any failure — caller decides
    whether to skip and continue or abort the batch.

    prompt_text is the caller's responsibility to source (the prompt
    library in the DB, or load_prompt_template() for the legacy
    file-based versions) — this function has no opinion on storage."""
    prompt = build_prompt(prompt_text, sig)

    try:
        result = generate_json(prompt, model=model, host=host)
    except OllamaError as exc:
        raise ExtractionError(f"story {sig.id}: {exc}") from exc

    raw_tags = result.get("tags")
    if not isinstance(raw_tags, list):
        raise ExtractionError(f"story {sig.id}: response missing 'tags' list: {result!r}")

    tags = _normalize_tags(raw_tags)
    if not (MIN_TAGS <= len(tags) <= MAX_TAGS):
        raise ExtractionError(
            f"story {sig.id}: got {len(tags)} tags after cleaning, expected "
            f"{MIN_TAGS}-{MAX_TAGS}: {tags!r}"
        )
    return tags
