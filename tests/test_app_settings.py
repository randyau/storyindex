from storyindex.app import app


def _client(tmp_path):
    app.config["DB_PATH"] = tmp_path / "t.sqlite"
    app.config["LIBRARIES_PATH"] = tmp_path / "libs.json"
    app.config["SETTINGS_PATH"] = tmp_path / "settings.json"
    return app.test_client()


def test_settings_page_shows_defaults(tmp_path):
    client = _client(tmp_path)
    r = client.get("/settings")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'value="dark"' in body or "checked" in body
    assert "http://localhost:11434" in body


def test_settings_post_updates_theme_and_reflects_in_nav(tmp_path):
    client = _client(tmp_path)
    r = client.post("/settings", data={
        "theme": "light",
        "ollama_host": "http://localhost:11434",
        "default_extract_model": "m1",
        "default_embed_model": "m2",
    })
    assert r.status_code == 302

    r = client.get("/")
    body = r.get_data(as_text=True)
    assert 'data-theme="light"' in body


def test_settings_post_persists_to_disk(tmp_path):
    client = _client(tmp_path)
    client.post("/settings", data={
        "theme": "light",
        "ollama_host": "http://box:9999",
        "default_extract_model": "m1",
        "default_embed_model": "m2",
    })
    from storyindex import settings

    data = settings.load(tmp_path / "settings.json")
    assert data["theme"] == "light"
    assert data["ollama_host"] == "http://box:9999"


def test_settings_post_persists_max_ctx_tokens(tmp_path):
    client = _client(tmp_path)
    client.post("/settings", data={
        "theme": "dark",
        "ollama_host": "http://localhost:11434",
        "default_extract_model": "m1",
        "default_embed_model": "m2",
        "max_ctx_tokens": "65536",
    })
    from storyindex import settings

    assert settings.load(tmp_path / "settings.json")["max_ctx_tokens"] == 65536


def test_settings_post_missing_max_ctx_tokens_falls_back_to_default(tmp_path):
    client = _client(tmp_path)
    client.post("/settings", data={
        "theme": "dark",
        "ollama_host": "http://localhost:11434",
        "default_extract_model": "m1",
        "default_embed_model": "m2",
    })
    from storyindex import settings

    assert settings.load(tmp_path / "settings.json")["max_ctx_tokens"] == settings.DEFAULTS["max_ctx_tokens"]
