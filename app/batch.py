"""Batch quality assessment: align 2..20 target logs against one reference.

Each target is run through the exact same pipeline as ``POST /align``
(:func:`app.io.run_alignment`).  The per-target quality indicators

* ``residual_rms``     -- large values are bad  (high side),
* ``mean_confidence``  -- small values are bad  (low side),
* ``matched_ratio``    -- ``matched_count / reference_count``; small bad,

are compared across the batch with **one-sided robust z-scores** based on
the batch median and MAD::

    z_high(x) = (x - median) / (1.4826 * MAD)
    z_low(x)  = (median - x) / (1.4826 * MAD)

The ``1.4826`` factor makes MAD a consistent estimator of the standard
deviation for normal data, so the usual ``z > 3`` outlier rule applies.

Degenerate batch (``MAD == 0``, i.e. more than half of the batch takes the
same value): the ratio would be infinite, so the score is reported as
``null`` and a value is flagged only when it lies on the *bad* side and
differs from the median (e.g. one large residual among identical ones).

Targets are ranked: anomalies first, then higher ``mean_confidence``, then
lower ``residual_rms`` (``null`` sorts last), with the input position as
the final tie-breaker, hence the ordering is stable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from statistics import median
from typing import Any, Sequence

from .io import AlignConfig, InputError, parse_events, run_alignment

MIN_TARGETS = 2
MAX_TARGETS = 20
Z_THRESHOLD = 3.0
MAD_NORMALISATION = 1.4826

_METRICS = ("residual_rms", "mean_confidence", "matched_ratio")
_HIGH_SIDE = {"residual_rms": True, "mean_confidence": False,
              "matched_ratio": False}


# --- request validation -----------------------------------------------------


@dataclass(frozen=True)
class BatchTarget:
    """One validated ``targets`` item: a name and its raw log payload."""

    name: str
    target: Any
    anchors: Any = None


def validate_batch_body(body: Any) -> tuple[Any, list[BatchTarget], AlignConfig]:
    """Validate a parsed JSON batch request body.

    Returns ``(reference, targets, config)``.  Raises :class:`InputError`
    (mapped to HTTP 400) with a message that pinpoints the offending
    field, e.g. ``targets[3] (line-d): ...``.
    """
    if not isinstance(body, dict):
        raise InputError(
            "JSON body must be an object with 'reference' and 'targets'"
        )
    if "reference" not in body:
        raise InputError("body requires 'reference'")
    if "targets" not in body:
        raise InputError("body requires 'targets'")

    raw_targets = body["targets"]
    if not isinstance(raw_targets, list):
        raise InputError("'targets' must be an array of target objects")
    if not (MIN_TARGETS <= len(raw_targets) <= MAX_TARGETS):
        raise InputError(
            f"'targets' must contain {MIN_TARGETS}..{MAX_TARGETS} items, "
            f"got {len(raw_targets)}"
        )

    targets: list[BatchTarget] = []
    seen_names: set[str] = set()
    for i, item in enumerate(raw_targets):
        if not isinstance(item, dict):
            raise InputError(f"targets[{i}]: entry must be an object")
        name = item.get("name")
        if not isinstance(name, str) or isinstance(name, bool) or not name.strip():
            raise InputError(
                f"targets[{i}]: 'name' must be a non-empty string"
            )
        if name in seen_names:
            raise InputError(f"targets[{i}]: duplicate target name {name!r}")
        if "target" not in item:
            raise InputError(
                f"targets[{i}] ({name}): missing 'target' log payload"
            )
        seen_names.add(name)
        targets.append(BatchTarget(
            name=name, target=item["target"], anchors=item.get("anchors"),
        ))

    cfg = _config_from_body(body)
    return body["reference"], targets, cfg


def _config_from_body(body: dict[str, Any]) -> AlignConfig:
    iterations = body.get("iterations", 3)
    if isinstance(iterations, bool) or not isinstance(iterations, int) \
            or not (1 <= iterations <= 10):
        raise InputError("'iterations' must be an integer in 1..10")
    time_weight = body.get("time_weight", 0.5)
    if isinstance(time_weight, bool) or not isinstance(time_weight, (int, float)) \
            or not (0.0 <= float(time_weight) <= 2.0):
        raise InputError("'time_weight' must be a number in 0..2")
    time_field = body.get("time_field")
    if time_field is not None and (
        isinstance(time_field, bool) or not isinstance(time_field, str)
        or not time_field.strip()
    ):
        raise InputError("'time_field' must be a non-empty string")
    label_field = body.get("label_field")
    if label_field is not None and (
        isinstance(label_field, bool) or not isinstance(label_field, str)
        or not label_field.strip()
    ):
        raise InputError("'label_field' must be a non-empty string")
    return AlignConfig(
        time_field=time_field or None,
        label_field=label_field or None,
        iterations=iterations,
        time_weight=float(time_weight),
    )


# --- robust statistics ------------------------------------------------------


def median_mad(values: Sequence[float | None]) -> tuple[float | None, float]:
    """Median and raw MAD of the non-null values; ``(None, 0.0)`` if empty."""
    nums = [v for v in values if v is not None]
    if not nums:
        return None, 0.0
    med = median(nums)
    return med, median(abs(v - med) for v in nums)


def one_sided_z(value: float | None, med: float | None, mad: float,
                high_side: bool) -> tuple[float | None, bool]:
    """One-sided robust z-score and the anomaly decision for one target.

    Returns ``(z, anomalous)``.  ``z`` is ``None`` when it cannot be
    computed: no numeric values in the batch (``med is None``), the value
    itself is ``None`` (e.g. ``residual_rms`` with zero anchors), or the
    MAD is zero and the value differs from the median (the ratio is
    infinite).  In that last case a value is flagged iff it sits on the
    bad side of the median.
    """
    if value is None or med is None:
        return None, False
    if mad == 0.0:
        if value == med:
            return 0.0, False
        differs_bad_side = (value > med) if high_side else (value < med)
        return None, bool(differs_bad_side)
    directed = (value - med) if high_side else (med - value)
    z = directed / (MAD_NORMALISATION * mad)
    return z, z > Z_THRESHOLD


# --- batch driver -----------------------------------------------------------


def run_batch(
    reference: Any, targets: Sequence[BatchTarget], base_cfg: AlignConfig,
) -> dict[str, Any]:
    """Align every target against ``reference`` and build the ranked report."""
    # Fail fast on a malformed reference with a locatable, target-agnostic
    # message; run_alignment() re-parses it per target below.
    try:
        parse_events(reference, base_cfg)
    except InputError as exc:
        raise InputError(f"reference: {exc}") from exc

    rows: list[dict[str, Any]] = []
    for i, item in enumerate(targets):
        cfg = AlignConfig(
            time_field=base_cfg.time_field,
            label_field=base_cfg.label_field,
            iterations=base_cfg.iterations,
            time_weight=base_cfg.time_weight,
            manual_anchors=item.anchors,
        )
        started = time.perf_counter()
        try:
            result = run_alignment(reference, item.target, cfg)
        except InputError as exc:
            raise InputError(f"targets[{i}] ({item.name}): {exc}") from exc
        elapsed = time.perf_counter() - started

        summary = result["summary"]
        ref_count = summary["reference_count"]
        matched = summary["matched_count"]
        rows.append({
            "input_index": i,
            "name": item.name,
            "reference_count": ref_count,
            "target_count": summary["target_count"],
            "matched_count": matched,
            "matched_ratio": (matched / ref_count) if ref_count else 0.0,
            "residual_rms": summary["residual_rms"],
            "mean_confidence": summary["mean_confidence"],
            "elapsed_seconds": round(elapsed, 6),
        })

    _score_and_rank(rows)

    statistics: dict[str, Any] = {}
    for metric in _METRICS:
        med, mad = median_mad([r[metric] for r in rows])
        statistics[metric] = {
            "median": med,
            "mad": mad,
            "normalized_scale": (MAD_NORMALISATION * mad) if med is not None
            else None,
        }

    return {
        "summary": {
            "target_count": len(rows),
            "anomalous_count": sum(1 for r in rows if r["anomalous"]),
            "z_threshold": Z_THRESHOLD,
        },
        "statistics": statistics,
        "results": rows,
    }


def _score_and_rank(rows: list[dict[str, Any]]) -> None:
    """Fill z-scores / anomaly flags in-place and order rows by the rules."""
    stats = {
        metric: median_mad([r[metric] for r in rows]) for metric in _METRICS
    }

    for r in rows:
        z_scores: dict[str, float | None] = {}
        reasons: list[str] = []
        worst: float = 0.0
        infinite = False
        for metric in _METRICS:
            med, mad = stats[metric]
            z, bad = one_sided_z(r[metric], med, mad, _HIGH_SIDE[metric])
            if z is None:
                z_scores[metric] = None
                if bad:
                    # MAD == 0 and the value differs on the bad side: the
                    # exact ratio is infinite; surfaced as null.
                    reasons.append(metric)
                    infinite = True
            else:
                z_scores[metric] = round(z, 6)
                if bad:
                    reasons.append(metric)
                if z > worst:
                    worst = z
        r["z_scores"] = z_scores
        r["anomaly_reasons"] = reasons
        r["anomalous"] = bool(reasons)
        r["max_z"] = (None if infinite else round(worst, 6))

    rows.sort(key=lambda r: (
        0 if r["anomalous"] else 1,
        -r["mean_confidence"],
        r["residual_rms"] if r["residual_rms"] is not None else float("inf"),
        r["input_index"],
    ))
    for rank, r in enumerate(rows, start=1):
        r["rank"] = rank
