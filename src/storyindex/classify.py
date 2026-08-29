"""Extraction pass: propose free-form tag candidates for a single story via
a local model. This is pass 1 of 2 — normalization/clustering into the
canonical tag vocabulary happens separately, after candidates accumulate.

Prompts are versioned files under prompts/, never inline strings, so a
prompt change is a diffable, re-runnable artifact rather than a silent
behavior change.
"""

from __future__ import annotations

from pathlib import Path

from storyindex.ollama_client import OllamaError, generate_json
from storyindex.signature import StorySignature

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"

MIN_TAGS = 1
MAX_TAGS = 20


class ExtractionError(RuntimeError):
    pass


def load_prompt_template(prompt_version: str) -> str:
    path = PROMPTS_DIR / f"extract_{prompt_version}.md"
    if not path.exists():
        raise ExtractionError(f"no prompt file for version {prompt_version!r}: {path}")
    return path.read_text(encoding="utf-8")


def build_prompt(template: str, sig: StorySignature) -> str:
    # Plain placeholder substitution, not str.format() — the prompt itself
    # contains literal JSON braces (the output-shape example) that .format
    # would misparse as format fields.
    return (
        template
        .replace("{title}", sig.title or "(untitled)")
        .replace("{author}", sig.author or "(unknown)")
        .replace("{body_text}", sig.body_text)
    )


def _normalize_tags(raw_tags: list) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for tag in raw_tags:
        if not isinstance(tag, str):
            continue
        t = tag.strip().lower()
        if not t or t in seen:
            continue
        seen.add(t)
        cleaned.append(t)
    return cleaned


def extract_tags(
    sig: StorySignature,
    model: str,
    prompt_version: str = "v1",
) -> list[str]:
    """Run the extraction pass for one story. Returns a deduped, cleaned
    list of candidate tag strings. Raises ExtractionError on any failure —
    caller decides whether to skip and continue or abort the batch."""
    template = load_prompt_template(prompt_version)
    prompt = build_prompt(template, sig)

    try:
        result = generate_json(prompt, model=model)
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
