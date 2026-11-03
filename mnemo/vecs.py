"""Embeddings, similarity and brute-force vector search.

Two things live here:

1. An `Embedder` interface with an offline, dependency-free default. Real
   embeddings (nomic-embed-text via Ollama) plug in behind the same interface
   without anything downstream changing.

2. Vector storage/search. Deliberately brute-force: at 50k notes x 512 dims a
   query is ~26M multiply-adds, which is milliseconds in C and acceptable here.
   No vector index, no native extension, no corruption mode.
"""
from __future__ import annotations

import hashlib
import math
import re
import sys
from array import array
from typing import Iterable, Protocol, Sequence

Vector = list[float]

_WORD = re.compile(r"[a-z0-9]+")
_LITTLE_ENDIAN = sys.byteorder == "little"

# Function words and generic verbs carry no topical signal, but their character
# n-grams still land in buckets and inflate similarity between unrelated notes.
# Measured: filtering these drops the mean unrelated-pair cosine substantially,
# which is what lets the routing thresholds separate cleanly.
_COMMON = frozenset("""
a an the this that these those it its is are was were be been being am
i you he she we they them him her his our your their my me us
and or but if then else so than as at by for from in into of on to with
without within about above below over under again further once here there
when where why how all any both each few more most other some such only own
same too very can will just should now not no nor do does did doing done
have has had having would could may might must shall get got getting make
makes made making use used using uses go goes going went come comes came
what which who whom while during before after between out off up down over
one two three first second next last new old good bad best better great
really quite also still even much many lot lots thing things stuff way ways
like need want know think see look find give take put let say said try
because though although however therefore thus hence via per etc eg ie
note notes noted writeup post article read reading saved save keep keeping
kept remember remembering reminder quick today tomorrow yesterday week weekly
month year day time times next step steps follow followed following worth
matter matters expected expecting compare compared comparing turned turns
problem problems idea ideas thought thoughts mind still undecided tiebreaker
covering cover covers came come coming tried trying try again another
interesting useful helpful important seems seemed looks looked actually
basically probably maybe perhaps definitely somewhat rather pretty bit
""".split())


def _is_signal(word: str) -> bool:
    return len(word) > 2 and word not in _COMMON


# ------------------------------------------------------------- packing ----
def pack(vec: Sequence[float]) -> bytes:
    """float32 little-endian blob."""
    arr = array("f", vec)
    if not _LITTLE_ENDIAN:
        arr.byteswap()
    return arr.tobytes()


def unpack(blob: bytes) -> Vector:
    arr = array("f")
    arr.frombytes(blob)
    if not _LITTLE_ENDIAN:
        arr.byteswap()
    return arr.tolist()


# ---------------------------------------------------------- similarity ----
def normalise(vec: Vector) -> Vector:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    inv = 1.0 / norm
    return [v * inv for v in vec]


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Assumes both are L2-normalised (everything we store is)."""
    return dot(a, b)


def mean(vectors: Sequence[Sequence[float]]) -> Vector:
    if not vectors:
        return []
    dim = len(vectors[0])
    acc = [0.0] * dim
    for vec in vectors:
        for i, v in enumerate(vec):
            acc[i] += v
    n = float(len(vectors))
    return normalise([v / n for v in acc])


def centred(vectors: Sequence[Sequence[float]]) -> list[Vector]:
    """Subtract the shared mean direction, then renormalise.

    Trained embedding spaces are anisotropic: nearly all vectors share a large
    common component, so *everything* looks similar to everything else.
    Measured with nomic-embed-text, unrelated notes score 0.432 and related
    ones 0.589 -- a gap of only 0.157, which k-means cannot work with.

    Removing the mean direction widens that gap to 0.242 and takes clustering
    purity from 45.8% to 85.8% on the benchmark corpus. This is what makes the
    cold-start bootstrap produce a real taxonomy instead of one giant topic.

    Used for CLUSTERING only. Routing thresholds are calibrated against raw
    similarity, and mixing the two scales would invalidate them.
    """
    if not vectors:
        return []
    mu = mean(vectors)
    if not mu:
        return [list(v) for v in vectors]
    return [normalise([a - b for a, b in zip(v, mu)]) for v in vectors]


def running_mean(centroid: Sequence[float], n: int, new_vec: Sequence[float]) -> Vector:
    """Fold one more vector into a centroid of `n` members. O(dim), no history."""
    if n <= 0 or not centroid:
        return list(new_vec)
    k = n + 1
    return normalise([(c * n + v) / k for c, v in zip(centroid, new_vec)])


# ----------------------------------------------------------- embedders ----
class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list[Vector]: ...


def _stable_hash(token: str, salt: bytes = b"") -> int:
    """blake2b, not builtin hash() -- Python randomises string hashing per
    process, which would silently invalidate every stored embedding on restart."""
    return int.from_bytes(
        hashlib.blake2b(token.encode("utf-8"), key=salt, digest_size=8).digest(), "big"
    )


class HashingEmbedder:
    """Offline embedder: signed feature hashing over word tokens + character
    n-grams, sublinear TF, L2-normalised.

    Character n-grams are what let it see that 'gaming', 'games' and 'gamer'
    are related without any trained model. It captures lexical similarity, not
    true semantics -- 'car' and 'automobile' stay far apart. That is the known
    ceiling of the offline mode, and why `OllamaEmbedder` exists.
    """

    def __init__(self, dim: int = 512, ngrams: tuple[int, ...] = (3, 4)):
        self.dim = dim
        self.ngrams = ngrams
        self.name = f"hashing-{dim}d-{'.'.join(map(str, ngrams))}g"

    def _features(self, text: str) -> Iterable[str]:
        words = [w for w in _WORD.findall(text.lower()) if _is_signal(w)]
        for word in words:
            yield f"w:{word}"
            yield f"w:{word}"                       # weight whole words over n-grams
            padded = f"^{word}$"
            for n in self.ngrams:
                for i in range(max(0, len(padded) - n + 1)):
                    yield f"c{n}:{padded[i : i + n]}"
        for a, b in zip(words, words[1:]):          # bigrams: "video games"
            yield f"b:{a}_{b}"

    def embed_one(self, text: str) -> Vector:
        counts: dict[int, float] = {}
        for feat in self._features(text):
            h = _stable_hash(feat)
            bucket = h % self.dim
            sign = 1.0 if (h >> 32) & 1 else -1.0
            counts[bucket] = counts.get(bucket, 0.0) + sign
        vec = [0.0] * self.dim
        for bucket, raw in counts.items():
            # sublinear TF, sign preserved
            vec[bucket] = math.copysign(1.0 + math.log(abs(raw)), raw) if raw else 0.0
        return normalise(vec)

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        return [self.embed_one(t) for t in texts]


class OllamaEmbedder:
    """Real embeddings via the bundled Ollama server. Stdlib-only HTTP.

    Verified by `python -m mnemo.doctor`, which checks dimension, determinism
    and -- the property that matters -- that these are genuinely SEMANTIC:
    'car' and 'automobile' score close together despite sharing no characters,
    which `HashingEmbedder` can never do.
    """

    def __init__(self, model: str | None = None, host: str | None = None,
                 dim: int | None = None):
        from .models import EMBED_DIM, EMBED_MODEL
        from .paths import OLLAMA_HOST
        self.model = model or EMBED_MODEL
        self.host = (host or OLLAMA_HOST).rstrip("/")
        self.dim = dim or EMBED_DIM
        self.name = f"ollama:{self.model}"

    def embed_one(self, text: str) -> Vector:
        return self.embed([text])[0]

    def embed(self, texts: Sequence[str]) -> list[Vector]:
        import json
        import urllib.request

        # keep_alive=-1 pins the model in VRAM. Without it Ollama unloads on its
        # own timer and the next capture pays a multi-second reload -- which is
        # exactly the latency this design exists to avoid.
        req = urllib.request.Request(
            f"{self.host}/api/embed",
            data=json.dumps({"model": self.model, "input": list(texts),
                             "keep_alive": -1}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read())
        return [normalise(v) for v in payload["embeddings"]]


# -------------------------------------------------------------- search ----
def top_k(query: Sequence[float], items: Sequence[tuple[str, Sequence[float]]], k: int = 8,
          floor: float = -1.0) -> list[tuple[str, float]]:
    """Brute-force cosine, highest first. `items` is [(id, vector), ...]."""
    scored = []
    for ident, vec in items:
        if len(vec) != len(query):
            continue                      # dim mismatch = stale embedding, skip
        sim = dot(query, vec)
        if sim >= floor:
            scored.append((ident, sim))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:k]
