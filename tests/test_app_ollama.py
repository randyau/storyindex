from storyindex import ollama_client
from storyindex.app import app


def _client(tmp_path):
    app.config["DB_PATH"] = tmp_path / "t.sqlite"
    app.config["LIBRARIES_PATH"] = tmp_path / "libs.json"
    app.config["SETTINGS_PATH"] = tmp_path / "settings.json"
    return app.test_client()


def test_ollama_status_not_running(tmp_path, monkeypatch):
    monkeypatch.setattr(ollama_client, "is_running", lambda host=None: False)
    client = _client(tmp_path)
    r = client.get("/ollama")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "not running" in body
    assert "qwen2.5:14b-instruct" in body  # recommendations always shown


def test_ollama_status_running_lists_models(tmp_path, monkeypatch):
    monkeypatch.setattr(ollama_client, "is_running", lambda host=None: True)
    monkeypatch.setattr(ollama_client, "list_models", lambda host=None: [{"name": "qwen2.5:14b-instruct"}])
    client = _client(tmp_path)
    r = client.get("/ollama")
    body = r.get_data(as_text=True)
    assert "running" in body
    assert "installed models" in body


def test_ollama_start_calls_start_server(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ollama_client, "start_server", lambda: calls.append(1))
    client = _client(tmp_path)
    r = client.post("/ollama/start")
    assert r.status_code == 302
    assert calls == [1]


def test_ollama_start_shows_error_instead_of_500_when_cli_missing(tmp_path, monkeypatch):
    def boom():
        raise ollama_client.OllamaError("the `ollama` CLI isn't installed or isn't on PATH")
    monkeypatch.setattr(ollama_client, "start_server", boom)
    client = _client(tmp_path)
    r = client.post("/ollama/start")
    assert r.status_code == 200
    assert "isn&#39;t installed" in r.get_data(as_text=True) or "isn't installed" in r.get_data(as_text=True)
