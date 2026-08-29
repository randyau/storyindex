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
