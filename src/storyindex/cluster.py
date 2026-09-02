"""Normalization pass (pass 2): fold free-form tag_candidates into a
canonical tag vocabulary via embedding similarity.

Deliberately simple, dependency-light greedy clustering rather than a full
hierarchical algorithm — this pass proposes clusters, it doesn't decide.
Human review (via the web app) is what actually crystallizes a cluster into
a kept/renamed/merged canonical tag; nothing here is meant to be final.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

from storyindex.ollama_client import DEFAULT_HOST, embed

DEFAULT_EMBED_MODEL = "nomic-embed-text"
DEFAULT_SIMILARITY_THRESHOLD = 0.82


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
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


def assign_embedded(
    clusters: list[Cluster],
    text: str,
    vec: list[float],
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> None:
    """Greedy-assign one already-embedded text to its most similar existing
    cluster (mutates `clusters` in place), or start a new one. Split out of
    cluster_tag_texts so a caller driving its own embedding calls one text
    at a time — e.g. scheduler.py, interleaving this job's calls with other
    jobs' round by round — can reuse the same assignment logic instead of
    needing every text embedded up front."""
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


def cluster_tag_texts(
    texts: list[str],
    model: str = DEFAULT_EMBED_MODEL,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    host: str = DEFAULT_HOST,
    on_embedded: Callable[[str], None] | None = None,
) -> list[Cluster]:
    """Greedy single-pass clustering: each text joins the most similar
    existing cluster if similarity exceeds threshold, else starts a new
    one. Order-dependent by design — good enough for a proposal pass that
    a human reviews, not meant to be a stable/optimal partition.

    Embedding (one network round trip per distinct text) is the dominant
    cost of this pass, not the clustering math itself - on_embedded, if
    given, is called with each text right after its embedding completes,
    so a caller can report progress incrementally through that phase
    instead of only after every text has been embedded and this function
    is ready to return a result."""
    clusters: list[Cluster] = []
    for text in texts:
        vec = embed(text, model=model, host=host)
        if on_embedded is not None:
            on_embedded(text)
        assign_embedded(clusters, text, vec, threshold)
    return clusters


def canonical_name(members: list[str], counts: dict[str, int]) -> str:
    """Pick the representative name for a cluster: most frequent underlying
    tag string across candidates, tie-broken by shortest then alphabetical."""
    return sorted(set(members), key=lambda t: (-counts.get(t, 0), len(t), t))[0]
