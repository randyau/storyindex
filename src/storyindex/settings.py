"""User-tweakable app settings: theme + Ollama connection + default models.

Same small-JSON-file pattern as libraries.py — one local file, not part of
any library's own sqlite (settings are a machine/user preference, not
data belonging to a particular story collection).
"""

from __future__ import annotations

import json
from pathlib import Path

from storyindex.ollama_client import DEFAULT_HOST

DEFAULT_CONFIG_PATH = Path.home() / ".storyindex" / "settings.json"

DEFAULTS = {
    "theme": "dark",
    "ollama_host": DEFAULT_HOST,
    "default_extract_model": "qwen2.5:7b-instruct",
    "default_embed_model": "nomic-embed-text",
    # Hardware ceiling for the extraction pass's context window: how much
    # of a long story classify.extract_tags can ask a model to read in one
    # call before it falls back to chunking. Actual GPU memory isn't
    # something this process can reliably auto-detect, so this is the
    # user's own stated budget - the shipped default fits comfortably on a
    # modest (~8GB) card; raise it here if your hardware can take more, so
    # a long story needs fewer chunks.
    "max_ctx_tokens": 32768,
}

VALID_THEMES = ("dark", "light")


def load(config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
    config_path = Path(config_path)
    data = dict(DEFAULTS)
    if not config_path.exists():
        return data
    try:
        stored = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return data
    for key in DEFAULTS:
        if key in stored:
            data[key] = stored[key]
    return data


def save(data: dict, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    config_path = Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def update(changes: dict, config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
    data = load(config_path)
    for key, value in changes.items():
        if key not in DEFAULTS:
            continue
        data[key] = value
    if data["theme"] not in VALID_THEMES:
        data["theme"] = DEFAULTS["theme"]
    save(data, config_path)
    return data
