"""Known-libraries registry: lets the running app switch which sqlite file
it's pointed at (a "library" = one story collection) without a bigger
multi-tenant/multi-connection system. A local user might have one library
per source they've pulled stories from.

Deliberately just a small JSON file next to the user's home dir, not
part of any single library's own sqlite file (it has to outlive/span
multiple of them).
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".storyindex" / "libraries.json"


def _empty() -> dict:
    return {"active": None, "libraries": {}}


def load(config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
    config_path = Path(config_path)
    if not config_path.exists():
        return _empty()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty()
    data.setdefault("active", None)
    data.setdefault("libraries", {})
    return data


def save(data: dict, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    config_path = Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def register(name: str, db_path: str, config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
    """Add (or update the path of) a named library, without changing which
    one is active."""
    data = load(config_path)
    data["libraries"][name] = db_path
    save(data, config_path)
    return data


def set_active(name: str, config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
    data = load(config_path)
    if name not in data["libraries"]:
        raise KeyError(f"no such library: {name!r}")
    data["active"] = name
    save(data, config_path)
    return data


def unregister(name: str, config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
    """Forgets a library (does not touch its sqlite file on disk — this
    only removes it from the switcher). Clears `active` if it was the
    active one; the caller is responsible for picking a new active
    library before the next request that needs one."""
    data = load(config_path)
    data["libraries"].pop(name, None)
    if data["active"] == name:
        data["active"] = next(iter(data["libraries"]), None)
    save(data, config_path)
    return data


def rename_library(old_name: str, new_name: str, config_path: Path = DEFAULT_CONFIG_PATH) -> dict:
    data = load(config_path)
    if old_name not in data["libraries"]:
        raise KeyError(f"no such library: {old_name!r}")
    data["libraries"][new_name] = data["libraries"].pop(old_name)
    if data["active"] == old_name:
        data["active"] = new_name
    save(data, config_path)
    return data


def ensure_registered_and_active(name: str, db_path: str, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Bootstrap: called at app startup with the --db path. Registers it
    under `name` if not already known, and makes it active if nothing else
    is (first run) — doesn't clobber an existing active choice."""
    data = load(config_path)
    if name not in data["libraries"]:
        data["libraries"][name] = db_path
    if data["active"] is None:
        data["active"] = name
    save(data, config_path)
