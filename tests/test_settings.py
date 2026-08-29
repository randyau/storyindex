from storyindex import settings


def test_load_returns_defaults_when_no_file(tmp_path):
    data = settings.load(tmp_path / "settings.json")
    assert data["theme"] == "dark"
    assert data["ollama_host"] == "http://localhost:11434"


def test_update_persists_and_round_trips(tmp_path):
    path = tmp_path / "settings.json"
    settings.update({"theme": "light", "ollama_host": "http://box:1234"}, path)
    data = settings.load(path)
    assert data["theme"] == "light"
    assert data["ollama_host"] == "http://box:1234"
    # untouched keys keep their defaults
    assert data["default_extract_model"] == settings.DEFAULTS["default_extract_model"]


def test_update_rejects_invalid_theme(tmp_path):
    path = tmp_path / "settings.json"
    settings.update({"theme": "neon"}, path)
    assert settings.load(path)["theme"] == "dark"


def test_load_ignores_corrupt_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("not json", encoding="utf-8")
    data = settings.load(path)
    assert data == settings.DEFAULTS
