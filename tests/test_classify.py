from storyindex import classify


def test_extract_tags_strips_category_label_leaked_into_tag(monkeypatch, make_sig):
    def fake_generate_json(prompt, model, host=None, max_ctx=None):
        return {"tags": ["road trip", "genre: mystery", "setting: rainy town"]}

    monkeypatch.setattr(classify, "generate_json", fake_generate_json)
    sig = make_sig("s1")
    tags = classify.extract_tags(sig, model="m", prompt_text="{title}{author}{body_text}")
    assert tags == ["road trip", "mystery", "rainy town"]


def test_extract_tags_dedupes_after_stripping_labels(monkeypatch, make_sig):
    def fake_generate_json(prompt, model, host=None, max_ctx=None):
        return {"tags": ["mystery", "genre: mystery"]}

    monkeypatch.setattr(classify, "generate_json", fake_generate_json)
    sig = make_sig("s1")
    tags = classify.extract_tags(sig, model="m", prompt_text="{title}{author}{body_text}")
    assert tags == ["mystery"]


def test_build_prompt_truncates_body_that_would_exceed_max_context(make_sig):
    # A story longer than MAX_BODY_CHARS must not push the instructions
    # (which come before {body_text} in every template) out of the model's
    # context window - truncate the story, never the instructions.
    sig = make_sig("s1", body="x" * (classify.MAX_BODY_CHARS + 5000))
    prompt = classify.build_prompt("INSTRUCTIONS\n{body_text}\nEND", sig)
    assert prompt.startswith("INSTRUCTIONS\n")
    assert prompt.endswith("[story truncated for length]\nEND")
    assert len(prompt) < len("INSTRUCTIONS\n") + classify.MAX_BODY_CHARS + 100


def test_build_prompt_leaves_short_body_untouched(make_sig):
    sig = make_sig("s1", body="a short story")
    prompt = classify.build_prompt("{body_text}", sig)
    assert prompt == "a short story"


def test_chunk_body_text_returns_single_chunk_when_it_fits():
    body = "para one.\n\npara two."
    assert classify._chunk_body_text(body, max_chars=1000) == [body]


def test_chunk_body_text_packs_paragraphs_greedily():
    # Each paragraph is 5 chars; max_chars=12 fits two ("aaaaa\n\nbbbbb" is
    # 12 chars) but not three - so this should produce the minimal chunk
    # count (2), not one paragraph per chunk.
    body = "\n\n".join(["aaaaa", "bbbbb", "ccccc", "ddddd"])
    chunks = classify._chunk_body_text(body, max_chars=12)
    assert chunks == ["aaaaa\n\nbbbbb", "ccccc\n\nddddd"]


def test_chunk_body_text_hard_slices_a_paragraph_longer_than_max_chars():
    body = "x" * 25
    chunks = classify._chunk_body_text(body, max_chars=10)
    assert chunks == ["x" * 10, "x" * 10, "x" * 5]


def test_chunk_body_text_hard_slice_mixed_with_normal_paragraphs():
    body = "short\n\n" + ("y" * 22) + "\n\nshort2"
    chunks = classify._chunk_body_text(body, max_chars=10)
    assert chunks == ["short", "y" * 10, "y" * 10, "y" * 2, "short2"]


def test_effective_max_ctx_tokens_capped_by_hardware_setting(monkeypatch):
    monkeypatch.setattr(classify.ollama_client, "model_max_context", lambda model, host=None: 131072)
    ctx = classify._effective_max_ctx_tokens("big-model", "http://x", hardware_ctx_cap=8192)
    assert ctx == 8192


def test_effective_max_ctx_tokens_capped_by_models_own_max(monkeypatch):
    monkeypatch.setattr(classify.ollama_client, "model_max_context", lambda model, host=None: 4096)
    ctx = classify._effective_max_ctx_tokens("small-model", "http://x", hardware_ctx_cap=131072)
    assert ctx == 4096


def test_effective_max_ctx_tokens_falls_back_to_hardware_cap_if_model_lookup_fails(monkeypatch):
    monkeypatch.setattr(classify.ollama_client, "model_max_context", lambda model, host=None: None)
    ctx = classify._effective_max_ctx_tokens("unknown-model", "http://x", hardware_ctx_cap=16384)
    assert ctx == 16384


def test_extract_tags_short_story_never_queries_models_own_context(monkeypatch, make_sig):
    # The common case (story fits under the hardware cap alone) shouldn't
    # pay for a model_max_context network round trip at all.
    def boom(model, host=None):
        raise AssertionError("should not be called for a story that already fits")

    monkeypatch.setattr(classify.ollama_client, "model_max_context", boom)
    monkeypatch.setattr(classify, "generate_json", lambda prompt, model, host=None, max_ctx=None: {"tags": ["x"]})
    sig = make_sig("s1", body="short story")
    assert classify.extract_tags(sig, model="m", prompt_text="{body_text}") == ["x"]


def test_extract_tags_chunks_long_story_and_merges_tags(monkeypatch, make_sig):
    monkeypatch.setattr(classify, "_max_body_chars", lambda max_ctx_tokens: 20)
    monkeypatch.setattr(classify.ollama_client, "model_max_context", lambda model, host=None: None)

    calls = []

    def fake_generate_json(prompt, model, host=None, max_ctx=None):
        calls.append(prompt)
        # First call gets "part 1", second gets "part 2", etc.
        n = len(calls)
        return {"tags": [f"tag-{n}", "shared"]}

    monkeypatch.setattr(classify, "generate_json", fake_generate_json)
    body = "\n\n".join(["one two three four five", "six seven eight nine ten", "eleven twelve"])
    sig = make_sig("s1", body=body)

    tags = classify.extract_tags(sig, model="m", prompt_text="{body_text}")

    assert len(calls) > 1  # actually split into multiple calls
    assert len(tags) == len(set(tags))  # deduped across chunks
    assert "shared" in tags and tags.count("shared") == 1
    assert all(f"part {i} of {len(calls)}" in calls[i - 1] for i in range(1, len(calls) + 1))


def test_extract_tags_single_chunk_after_recompute_skips_part_note(monkeypatch, make_sig):
    # If recomputing against the model's real max context (distinct from
    # the hardware-only ceiling) turns out big enough that everything still
    # fits in one chunk, no "part N of M" note should be injected - that's
    # only for genuine multi-chunk splits.
    monkeypatch.setattr(classify, "_max_body_chars", lambda t: 5 if t == 100_000 else 1000)
    monkeypatch.setattr(classify.ollama_client, "model_max_context", lambda model, host=None: 50_000)

    captured = {}

    def fake_generate_json(prompt, model, host=None, max_ctx=None):
        captured["prompt"] = prompt
        return {"tags": ["x"]}

    monkeypatch.setattr(classify, "generate_json", fake_generate_json)
    sig = make_sig("s1", body="a longer body that exceeds five characters")
    tags = classify.extract_tags(sig, model="m", prompt_text="{body_text}", max_ctx_tokens=100_000)
    assert tags == ["x"]
    assert "part 1 of" not in captured["prompt"]
