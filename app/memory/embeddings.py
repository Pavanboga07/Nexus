"""Deterministic local embedding (V6, no network, no model).

A hashed bag-of-words vector: each lowercase word token hashes
(sha256) to one of ``DIM`` buckets and adds 1.0; the vector is then
L2-normalized. Texts sharing words are close under L2 distance, which
is all the sqlite-vec leg needs for hybrid recall (the FTS5 leg
covers exact keywords). Not semantic — v1 deliberately avoids a
model download. ``DIM`` is part of the on-disk format: changing it
requires re-embedding (see ``import_json`` which re-embeds anyway).
"""

from __future__ import annotations

import hashlib
import math
import re

DIM = 64

_TOKEN = re.compile(r"[a-z0-9']+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def embed(text: str) -> list[float]:
    vec = [0.0] * DIM
    for token in tokenize(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        vec[int.from_bytes(digest[:4], "little") % DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


__all__ = ["DIM", "embed", "tokenize"]
