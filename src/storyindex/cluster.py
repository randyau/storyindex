"""Normalization pass (pass 2): fold free-form tag_candidates into a
canonical tag vocabulary via embedding similarity.

Deliberately simple, dependency-light greedy clustering rather than a full
hierarchical algorithm — this pass proposes clusters, it doesn't decide.
Human review (via the web app) is what actually crystallizes a cluster into
a kept/renamed/merged canonical tag; nothing here is meant to be final.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from storyindex.ollama_client import embed

DEFAULT_EMBED_MODEL = "nomic-embed-text"
DEFAULT_SIMILARITY_THRESHOLD = 0.82


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _mean_vec(vecs: list[list[float]]) -> list[float]:
    n = len(vecs)
    dim = len(vecs[0])
    return [sum(v[i] for v in vecs) / n for i in range(dim)]


@dataclass
class Cluster:
    members: list[str] = field(default_factory=list)
    vectors: list[list[float]] = field(default_factory=list)
    centroid: list[float] = field(default_factory=list)

    def add(self, text: str, vec: list[float]) -> None:
        self.members.append(text)
        self.vectors.append(vec)
        self.centroid = _mean_vec(self.vectors)


def cluster_tag_texts(
    texts: list[str],
    model: str = DEFAULT_EMBED_MODEL,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> list[Cluster]:
    """Greedy single-pass clustering: each text joins the most similar
    existing cluster if similarity exceeds threshold, else starts a new
    one. Order-dependent by design — good enough for a proposal pass that
    a human reviews, not meant to be a stable/optimal partition."""
    clusters: list[Cluster] = []
    for text in texts:
        vec = embed(text, model=model)
        best_cluster = None
        best_sim = -1.0
        for c in clusters:
            sim = _cosine(vec, c.centroid)
            if sim > best_sim:
                best_sim = sim
                best_cluster = c
        if best_cluster is not None and best_sim >= threshold:
            best_cluster.add(text, vec)
        else:
            c = Cluster()
            c.add(text, vec)
            clusters.append(c)
    return clusters


def canonical_name(members: list[str], counts: dict[str, int]) -> str:
    """Pick the representative name for a cluster: most frequent underlying
    tag string across candidates, tie-broken by shortest then alphabetical."""
    return sorted(set(members), key=lambda t: (-counts.get(t, 0), len(t), t))[0]
