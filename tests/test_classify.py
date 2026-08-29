from storyindex import classify


def test_extract_tags_strips_category_label_leaked_into_tag(monkeypatch, make_sig):
    def fake_generate_json(prompt, model, host=None):
        return {"tags": ["road trip", "genre: mystery", "setting: rainy town"]}

    monkeypatch.setattr(classify, "generate_json", fake_generate_json)
    sig = make_sig("s1")
    tags = classify.extract_tags(sig, model="m", prompt_text="{title}{author}{body_text}")
    assert tags == ["road trip", "mystery", "rainy town"]


def test_extract_tags_dedupes_after_stripping_labels(monkeypatch, make_sig):
    def fake_generate_json(prompt, model, host=None):
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
