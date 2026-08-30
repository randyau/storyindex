from storyindex import cluster


def _fake_embed(monkeypatch, vectors):
    def fake(text, model, host=None):
        return vectors[text]

    monkeypatch.setattr(cluster, "embed", fake)


def test_cluster_tag_texts_calls_on_embedded_once_per_text_in_order(monkeypatch):
    _fake_embed(monkeypatch, {"a": [1.0, 0.0], "b": [0.0, 1.0], "c": [1.0, 0.0]})
    seen = []
    cluster.cluster_tag_texts(["a", "b", "c"], on_embedded=seen.append)
    assert seen == ["a", "b", "c"]


def test_cluster_tag_texts_on_embedded_fires_before_clustering_decision(monkeypatch):
    # on_embedded must fire for every text as it's embedded, not only for
    # texts that end up starting a new cluster - a caller reporting
    # progress cares about "was this text's embedding computed yet", not
    # how clustering later groups it.
    _fake_embed(monkeypatch, {"a": [1.0, 0.0], "b": [1.0, 0.0], "c": [1.0, 0.0]})
    seen = []
    clusters = cluster.cluster_tag_texts(["a", "b", "c"], threshold=0.5, on_embedded=seen.append)
    assert seen == ["a", "b", "c"]
    assert len(clusters) == 1  # all three merged into one cluster
    assert sorted(clusters[0].members) == ["a", "b", "c"]


def test_cluster_tag_texts_works_without_on_embedded(monkeypatch):
    _fake_embed(monkeypatch, {"a": [1.0], "b": [0.0]})
    clusters = cluster.cluster_tag_texts(["a", "b"], threshold=0.9)
    assert len(clusters) == 2
