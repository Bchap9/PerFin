# SPDX-License-Identifier: AGPL-3.0-or-later
"""The heuristic kind classifier: binarised multinomial naive Bayes over tokens.

Pure Python, deterministic, no network and no language model (ADR-0007).
Trained offline by ``scripts/train_classifier.py`` on synthetic documents
from ``tests/fixtures/synth_classify.py`` and shipped as a versioned JSON
artefact (``artefacts/kind_nb_v1.json``); a test retrains it and requires the
same bytes, so the artefact is always reproducible from the generator.

Features are the distinct unigrams and bigrams of
:func:`app.ingestion.classify.text.tokens` (digits by shape). The score of a
class is its log prior plus ``scale`` times the *mean* log likelihood of the
document's known features, damped by ``n / min_evidence`` when it has fewer
than ``min_evidence`` of them: long and short documents are comparable, and
a few words ("Total 12.00") give a modest confidence rather than certainty.
Unknown features are ignored; a document with none is not classified.
"""

from __future__ import annotations

import itertools
import json
import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.ingestion.classify.text import tokens

FORMAT: Final = "perfin-kind-nb"
ARTEFACT: Final = "kind_nb_v1.json"
MAX_ARTEFACT_BYTES: Final = 4 * 1024 * 1024
MAX_FEATURES: Final = 4000
MIN_DOC_FREQ: Final = 3
ALPHA: Final = 0.5
SCALE: Final = 3.0
#: Fewer known features than this weaken the evidence proportionally.
MIN_EVIDENCE: Final = 30
KINDS: Final = ("statement", "receipt", "invoice", "tax_form", "payslip", "insurance", "other")
#: Features that are only formatting noise.
_STOP: Final = frozenset({"the", "of", "and", "to", "a", "in", "for", "on", "is", "at", "by"})


def features(text: str) -> set[str]:
    """The distinct unigram and bigram features of ``text``."""
    toks = [t for t in tokens(text) if t not in _STOP]
    out = set(toks)
    out.update(f"{a}_{b}" for a, b in itertools.pairwise(toks))
    return out


class KindModel(BaseModel):
    """The JSON artefact (validated on load)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format: Literal["perfin-kind-nb"]
    format_version: Literal[1]
    model_version: Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")]
    classes: list[Annotated[str, Field(max_length=32)]] = Field(min_length=2, max_length=16)
    scale: float = Field(gt=0, le=1000)
    min_evidence: int = Field(ge=1, le=10_000)
    log_prior: list[float]
    #: feature -> log P(feature | class), one value per class.
    features: dict[Annotated[str, Field(max_length=80)], list[float]] = Field(
        max_length=MAX_FEATURES
    )
    training: dict[str, str | int | float]

    @model_validator(mode="after")
    def _shapes(self) -> KindModel:
        width = len(self.classes)
        if len(self.log_prior) != width:
            raise ValueError("log_prior has one value per class")
        if any(len(v) != width for v in self.features.values()):
            raise ValueError("every feature has one value per class")
        if any(not math.isfinite(x) for v in self.features.values() for x in v):
            raise ValueError("non-finite weight")
        return self


@dataclass(frozen=True)
class Prediction:
    kind: str | None
    confidence: float
    scores: dict[str, float]
    known_features: int


def train(
    samples: Iterable[tuple[str, str]],
    *,
    model_version: str,
    training: dict[str, str | int | float],
    classes: Sequence[str] = KINDS,
) -> KindModel:
    """Fit the model on ``(text, kind)`` pairs (deterministic for the same input)."""
    doc_count: Counter[str] = Counter()
    df: Counter[str] = Counter()
    per_class: dict[str, Counter[str]] = {c: Counter() for c in classes}
    rows: list[tuple[str, set[str]]] = []
    for text, kind in samples:
        if kind not in per_class:
            raise ValueError(f"unknown class {kind}")
        feats = features(text)
        rows.append((kind, feats))
        doc_count[kind] += 1
        df.update(feats)
    eligible = [f for f, n in df.items() if n >= MIN_DOC_FREQ]
    eligible.sort(key=lambda f: (-df[f], f))
    vocab = sorted(eligible[:MAX_FEATURES])
    keep = set(vocab)
    for kind, feats in rows:
        per_class[kind].update(feats & keep)
    total_docs = sum(doc_count.values())
    log_prior = [
        round(math.log((doc_count[c] + 1) / (total_docs + len(classes))), 6) for c in classes
    ]
    totals = {c: sum(per_class[c].values()) for c in classes}
    weights: dict[str, list[float]] = {}
    for feat in vocab:
        weights[feat] = [
            round(
                math.log((per_class[c][feat] + ALPHA) / (totals[c] + ALPHA * len(vocab))),
                6,
            )
            for c in classes
        ]
    return KindModel(
        format=FORMAT,
        format_version=1,
        model_version=model_version,
        classes=list(classes),
        scale=SCALE,
        min_evidence=MIN_EVIDENCE,
        log_prior=log_prior,
        features=weights,
        training=training,
    )


def dumps(model: KindModel) -> str:
    """Canonical JSON (sorted keys, one trailing newline): stable bytes for the same model."""
    return json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"


def loads(raw: bytes) -> KindModel:
    if len(raw) > MAX_ARTEFACT_BYTES:
        raise ValueError("classifier artefact too large")
    return KindModel.model_validate_json(raw)


@cache
def shipped_model() -> KindModel:
    """The artefact shipped in the package (validated once per process)."""
    raw = resources.files("app.ingestion.classify").joinpath("artefacts", ARTEFACT).read_bytes()
    return loads(raw)


def predict(model: KindModel, text: str) -> Prediction:
    feats = [model.features[f] for f in features(text) if f in model.features]
    if not feats:
        return Prediction(None, 0.0, {}, 0)
    width = len(model.classes)
    weight = model.scale / max(len(feats), model.min_evidence)
    scores = [model.log_prior[i] + weight * sum(v[i] for v in feats) for i in range(width)]
    top = max(scores)
    exps = [math.exp(s - top) for s in scores]
    total = sum(exps)
    probs = {model.classes[i]: round(exps[i] / total, 4) for i in range(width)}
    best = max(range(width), key=lambda i: (scores[i], -i))
    return Prediction(model.classes[best], probs[model.classes[best]], probs, len(feats))
