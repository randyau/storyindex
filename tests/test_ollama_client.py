import pytest
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
    with pytest.raises(ollama_client.OllamaError):
        ollama_client.list_models()


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


def test_start_server_raises_ollama_error_when_cli_missing(monkeypatch):
    import subprocess

    def boom(*args, **kwargs):
        raise FileNotFoundError("no such file: ollama")
    monkeypatch.setattr(subprocess, "Popen", boom)
    with pytest.raises(ollama_client.OllamaError, match="ollama"):
        ollama_client.start_server()


def test_recommended_models_includes_embedding_model():
    names = [m["name"] for m in ollama_client.RECOMMENDED_MODELS]
    assert "nomic-embed-text" in names


def test_generate_json_sizes_num_ctx_to_prompt_length(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured["options"] = json["options"]
        return FakeResponse({"response": '{"tags": ["a"]}'})

    monkeypatch.setattr(requests, "post", fake_post)
    ollama_client.generate_json("short prompt", model="m")
    assert captured["options"]["num_ctx"] == 4096

    monkeypatch.setattr(requests, "post", fake_post)
    ollama_client.generate_json("x" * 40000, model="m")
    assert captured["options"]["num_ctx"] == 16384


def test_generate_json_num_ctx_never_exceeds_max_ctx_argument(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured["options"] = json["options"]
        return FakeResponse({"response": '{"tags": ["a"]}'})

    monkeypatch.setattr(requests, "post", fake_post)
    # A prompt long enough to want the 16384 bucket, but max_ctx caps it at
    # 8192 - the resulting num_ctx must never exceed what the caller allowed.
    ollama_client.generate_json("x" * 40000, model="m", max_ctx=8192)
    assert captured["options"]["num_ctx"] == 8192


def test_estimate_num_ctx_picks_largest_bucket_when_needed_exceeds_ceiling():
    # needed tokens is way beyond every bucket <= max_ctx - falls back to
    # the largest usable bucket rather than raising or exceeding max_ctx.
    assert ollama_client._estimate_num_ctx("x" * 1_000_000, max_ctx=8192) == 8192


def test_model_max_context_reads_context_length_from_show_endpoint(monkeypatch):
    ollama_client.model_max_context.cache_clear()

    def fake_post(url, json, timeout):
        assert url.endswith("/api/show")
        assert json == {"model": "qwen-test-1"}
        return FakeResponse({"model_info": {"qwen2.context_length": 32768, "qwen2.other": 1}})

    monkeypatch.setattr(requests, "post", fake_post)
    assert ollama_client.model_max_context("qwen-test-1") == 32768


def test_model_max_context_returns_none_on_request_failure(monkeypatch):
    ollama_client.model_max_context.cache_clear()

    def boom(url, json, timeout):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(requests, "post", boom)
    assert ollama_client.model_max_context("unreachable-model-1") is None


def test_model_max_context_returns_none_when_field_missing(monkeypatch):
    ollama_client.model_max_context.cache_clear()
    monkeypatch.setattr(
        requests, "post", lambda url, json, timeout: FakeResponse({"model_info": {"unrelated": 1}})
    )
    assert ollama_client.model_max_context("no-ctx-field-model-1") is None


def test_model_max_context_is_cached_per_model_host(monkeypatch):
    ollama_client.model_max_context.cache_clear()
    calls = []

    def fake_post(url, json, timeout):
        calls.append(json["model"])
        return FakeResponse({"model_info": {"llama.context_length": 8192}})

    monkeypatch.setattr(requests, "post", fake_post)
    assert ollama_client.model_max_context("cached-model-1") == 8192
    assert ollama_client.model_max_context("cached-model-1") == 8192
    assert len(calls) == 1  # second call served from cache, no new request
