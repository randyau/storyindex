import requests

from storyindex import ollama_client


class FakeResponse:
    def __init__(self, json_data, status=200):
        self._json = json_data
        self.status_code = status
        self.text = str(json_data)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._json


def test_is_running_true_on_success(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda url, timeout: FakeResponse({}))
    assert ollama_client.is_running() is True


def test_is_running_false_on_connection_error(monkeypatch):
    def boom(url, timeout):
        raise requests.ConnectionError("refused")
    monkeypatch.setattr(requests, "get", boom)
    assert ollama_client.is_running() is False


def test_list_models_returns_models_list(monkeypatch):
    monkeypatch.setattr(
        requests, "get",
        lambda url, timeout: FakeResponse({"models": [{"name": "qwen2.5:14b-instruct"}]}),
    )
    models = ollama_client.list_models()
    assert models == [{"name": "qwen2.5:14b-instruct"}]


def test_list_models_raises_ollama_error_on_failure(monkeypatch):
    def boom(url, timeout):
        raise requests.ConnectionError("refused")
    monkeypatch.setattr(requests, "get", boom)
    try:
        ollama_client.list_models()
        assert False, "expected OllamaError"
    except ollama_client.OllamaError:
        pass


def test_start_server_spawns_detached_process(monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

    import subprocess
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    ollama_client.start_server()
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == ["ollama", "serve"]
    assert kwargs.get("start_new_session") is True


def test_recommended_models_includes_embedding_model():
    names = [m["name"] for m in ollama_client.RECOMMENDED_MODELS]
    assert "nomic-embed-text" in names
