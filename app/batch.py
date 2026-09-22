"""Batch alignment quality assessment for ``POST /align/batch``.

One reference log is aligned against 2..20 named target logs using exactly
the same pipeline as ``POST /align``.  For every target the response carries
the headline quality metrics

* ``residual_rms``     -- RMS anchor residual (seconds), high is bad;
* ``mean_confidence``  -- mean anchor confidence in [0, 1], low is bad;
* ``matched_count`` / ``matched_ratio`` -- how much of the target matched.

Outliers are scored with *one-sided robust z-scores* against the batch
itself:

    median = median(values)
    MAD    = median(|value - median|)
    z      = 0.6745 * (value - median) / MAD          (normalised side only)

* ``residual_rms`` uses the *high* side (z forced to 0 below the median);
* ``mean_confidence`` and ``matched_ratio`` use the *low* side.

When the MAD is 0 the scale is degenerate: a value is flagged as an outlier
only when it differs from the median (on the scored side); its ``z_*`` field
is then reported as ``null`` because no finite z exists.  A target with no
anchors has ``residual_rms = null`` and ``z_residual_rms = null`` and never
flags that metric.
"""

from __future__ import annotations

import math
from statistics import median
from typing import Any, Optional, Sequence

from .io import (
    AlignConfig,
    InputError,
    align_events,
    parse_events,
)
from .matching import DEFAULT_ITERATIONS, DEFAULT_TIME_WEIGHT

MIN_TARGETS = 2
MAX_TARGETS = 20
Z_THRESHOLD = 3.0
MAD_NORMALIZER = 0.6745        # 1 / 1.4826, i.e. MAD of a unit normal


# --- robust statistics ------------------------------------------------------


def median_mad(values: Sequence[float]) -> tuple[Optional[float], Optional[float]]:
    """Return ``(median, MAD)`` over the non-``None`` values.

    Both are ``None`` when every value is missing.
    """
    present = [v for v in values if v is not None]
    if not present:
        return None, None
    med = median(present)
    mad = median(abs(v - med) for v in present)
    return med, mad


def one_sided_z(value: Optional[float], med: Optional[float],
                mad: Optional[float], *, high_side: bool) -> tuple[
                    Optional[float], bool]:
    """One-sided MAD-based robust z-score.

    Returns ``(z, anomalous)``.  The z-score is clamped to 0 on the
    non-scored side.  When ``MAD == 0`` the z is ``null`` and the value is
    anomalous iff it differs from the median on the scored side.
    ``(None, False)`` is returned when the value or the statistic is
    unavailable.
    """
    if value is None or med is None or mad is None:
        return None, False
    deviation = value - med
    if high_side:
        if deviation <= 0.0:
            return 0.0, False
    else:
        if deviation >= 0.0:
            return 0.0, False
    if mad == 0.0:
        return None, deviation != 0.0
    z = MAD_NORMALIZER * deviation / mad
    # On the scored side the deviation already has the right sign
    # (positive high-side, negative low-side), so flag by magnitude.
    return z, abs(z) > Z_THRESHOLD


# --- batch driver -----------------------------------------------------------


def assess_batch(
    refs_raw: Any,
    targets: Sequence[dict[str, Any]],
    cfg: AlignConfig,
) -> dict[str, Any]:
    """Align every target against one reference and rank the quality.

    ``targets`` is a sequence of ``{"name", "target", "anchors"?}`` dicts
    already validated by :func:`parse_batch_request` (per-target ``anchors``
    override the shared config; the other config fields are shared).
    """
    # The reference is parsed once; every target reuses the same Event list.
    refs = parse_events(refs_raw, cfg)

    rows: list[dict[str, Any]] = []
    for order, item in enumerate(targets):
        name = item["name"]
        try:
            tgts = parse_events(item["target"], cfg)
            if not refs and not tgts:
                raise InputError("both reference and target streams are empty")
            item_cfg = (
                AlignConfig(
                    time_field=cfg.time_field,
                    label_field=cfg.label_field,
                    iterations=cfg.iterations,
                    time_weight=cfg.time_weight,
                    manual_anchors=item["anchors"],
                )
                if "anchors" in item
                else cfg
            )
            result = align_events(refs, tgts, item_cfg)
        except InputError as exc:
            raise InputError(f"targets[{order}] ({name!r}): {exc}") from exc

        summary = result["summary"]
        target_count = summary["target_count"]
        matched = summary["matched_count"]
        row = {
            "name": name,
            "residual_rms": summary["residual_rms"],
            "mean_confidence": summary["mean_confidence"],
            "matched_count": matched,
            "target_count": target_count,
            "matched_ratio": matched / target_count if target_count else 0.0,
            "reference_count": summary["reference_count"],
            "inserted_count": summary["inserted_count"],
            "missing_count": summary["missing_count"],
            "manual_anchor_count": summary["manual_anchor_count"],
            "_order": order,
        }
        rows.append(row)

    _annotate_outliers(rows)

    # Ranking: anomalies first, then higher confidence, then smaller
    # residuals, then the input order (stable).
    rows.sort(key=lambda r: (
        not r["anomalous"],
        -r["mean_confidence"],
        r["residual_rms"] if r["residual_rms"] is not None else float("inf"),
        r["_order"],
    ))

    statistics = _batch_statistics(rows)
    results: list[dict[str, Any]] = []
    for rank, row in enumerate(rows, start=1):
        order = row.pop("_order")
        results.append({
            "rank": rank,
            "name": row["name"],
            "residual_rms": _round(row["residual_rms"]),
            "mean_confidence": _round(row["mean_confidence"]),
            "matched_count": row["matched_count"],
            "target_count": row["target_count"],
            "matched_ratio": _round(row["matched_ratio"]),
            "reference_count": row["reference_count"],
            "inserted_count": row["inserted_count"],
            "missing_count": row["missing_count"],
            "manual_anchor_count": row["manual_anchor_count"],
            "z_residual_rms": _round(row["z_residual_rms"]),
            "z_mean_confidence": _round(row["z_mean_confidence"]),
            "z_matched_ratio": _round(row["z_matched_ratio"]),
            "anomaly_reasons": row["anomaly_reasons"],
            "anomalous": row["anomalous"],
            "input_index": order,
        })

    return {
        "summary": {
            "target_count": len(results),
            "anomaly_count": sum(1 for r in results if r["anomalous"]),
            "z_threshold": Z_THRESHOLD,
            "statistics": statistics,
        },
        "results": results,
    }


def _annotate_outliers(rows: list[dict[str, Any]]) -> None:
    """Fill the ``z_*`` / ``anomaly_reasons`` / ``anomalous`` fields."""
    stats: dict[str, tuple[Optional[float], Optional[float]]] = {
        "residual_rms": median_mad([r["residual_rms"] for r in rows]),
        "mean_confidence": median_mad([r["mean_confidence"] for r in rows]),
        "matched_ratio": median_mad([r["matched_ratio"] for r in rows]),
    }

    for row in rows:
        reasons: list[str] = []

        z, bad = one_sided_z(
            row["residual_rms"], *stats["residual_rms"], high_side=True)
        row["z_residual_rms"] = z
        if bad:
            reasons.append("residual_rms_high")

        z, bad = one_sided_z(
            row["mean_confidence"], *stats["mean_confidence"], high_side=False)
        row["z_mean_confidence"] = z
        if bad:
            reasons.append("mean_confidence_low")

        z, bad = one_sided_z(
            row["matched_ratio"], *stats["matched_ratio"], high_side=False)
        row["z_matched_ratio"] = z
        if bad:
            reasons.append("matched_ratio_low")

        row["anomaly_reasons"] = reasons
        row["anomalous"] = bool(reasons)


def _batch_statistics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Median / MAD per metric (computed over the unordered metric set)."""
    out: dict[str, Any] = {}
    for key in ("residual_rms", "mean_confidence", "matched_ratio"):
        med, mad = median_mad([r[key] for r in rows])
        out[key] = {"median": _round(med), "mad": _round(mad)}
    return out


def _round(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), 6)


# --- request validation -----------------------------------------------------


def parse_batch_request(body: Any) -> tuple[Any, list[dict[str, Any]], AlignConfig]:
    """Validate a ``/align/batch`` JSON body.

    Returns ``(reference_raw, targets, config)``; raises :class:`InputError`
    with a locatable message on any problem.
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
            f"'targets' must contain {MIN_TARGETS}..{MAX_TARGETS} entries, "
            f"got {len(raw_targets)}"
        )

    targets: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for i, item in enumerate(raw_targets):
        where = f"targets[{i}]"
        if not isinstance(item, dict):
            raise InputError(f"{where}: target entry must be an object")
        if "name" not in item:
            raise InputError(f"{where}: missing required field 'name'")
        name = item["name"]
        if not isinstance(name, str) or not name.strip():
            raise InputError(
                f"{where}: 'name' must be a non-empty string"
            )
        name = name.strip()
        if name in seen_names:
            raise InputError(f"{where}: duplicate target name {name!r}")
        seen_names.add(name)
        if "target" not in item:
            raise InputError(
                f"{where} ({name!r}): missing required field 'target'"
            )

        target = {
            "name": name,
            "target": item["target"],
        }
        if "anchors" in item:
            target["anchors"] = item["anchors"]
        targets.append(target)

    cfg = AlignConfig(
        time_field=_optional_str(body, "time_field"),
        label_field=_optional_str(body, "label_field"),
        iterations=_bounded_int(
            body, "iterations", default=DEFAULT_ITERATIONS, lo=1, hi=10),
        time_weight=_bounded_float(
            body, "time_weight", default=DEFAULT_TIME_WEIGHT, lo=0.0, hi=2.0),
    )
    return body["reference"], targets, cfg


def _optional_str(body: dict[str, Any], key: str) -> Optional[str]:
    if key not in body or body[key] is None:
        return None
    value = body[key]
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{key!r} must be a non-empty string")
    return value.strip()


def _bounded_int(body: dict[str, Any], key: str, *, default: int,
                 lo: int, hi: int) -> int:
    if key not in body or body[key] is None:
        return default
    value = body[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(f"{key!r} must be an integer in [{lo}, {hi}]")
    if not (lo <= value <= hi):
        raise InputError(f"{key!r} must be within [{lo}, {hi}], got {value}")
    return value


def _bounded_float(body: dict[str, Any], key: str, *, default: float,
                   lo: float, hi: float) -> float:
    if key not in body or body[key] is None:
        return default
    value = body[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{key!r} must be a number in [{lo}, {hi}]")
    value = float(value)
    if not math.isfinite(value) or not (lo <= value <= hi):
        raise InputError(f"{key!r} must be within [{lo}, {hi}], got {value}")
    return value
