import pytest

from storyindex import libraries


def test_load_missing_file_returns_empty(tmp_path):
    data = libraries.load(tmp_path / "nope.json")
    assert data == {"active": None, "libraries": {}}


def test_load_accepts_str_path(tmp_path):
    cfg = str(tmp_path / "libs.json")
    libraries.register("a", "/a.sqlite", cfg)
    assert libraries.load(cfg)["libraries"]["a"] == "/a.sqlite"


def test_register_then_load_roundtrip(tmp_path):
    cfg = tmp_path / "libs.json"
    libraries.register("default", "/a/b.sqlite", cfg)
    data = libraries.load(cfg)
    assert data["libraries"]["default"] == "/a/b.sqlite"


def test_set_active_requires_known_library(tmp_path):
    cfg = tmp_path / "libs.json"
    libraries.register("a", "/a.sqlite", cfg)
    libraries.set_active("a", cfg)
    assert libraries.load(cfg)["active"] == "a"

    with pytest.raises(KeyError):
        libraries.set_active("missing", cfg)


def test_ensure_registered_and_active_first_run_activates(tmp_path):
    cfg = tmp_path / "libs.json"
    libraries.ensure_registered_and_active("default", "/a.sqlite", cfg)
    data = libraries.load(cfg)
    assert data["active"] == "default"
    assert data["libraries"]["default"] == "/a.sqlite"


def test_ensure_registered_and_active_does_not_clobber_existing_active(tmp_path):
    cfg = tmp_path / "libs.json"
    libraries.register("first", "/first.sqlite", cfg)
    libraries.set_active("first", cfg)
    libraries.ensure_registered_and_active("second", "/second.sqlite", cfg)
    data = libraries.load(cfg)
    assert data["active"] == "first"
    assert "second" in data["libraries"]
