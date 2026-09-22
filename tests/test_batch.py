"""Tests for POST /align/batch and the robust batch scoring (app.batch)."""

from __future__ import annotations

import random

import pytest
from fastapi.testclient import TestClient

from app.batch import (
    MAX_TARGETS,
    Z_THRESHOLD,
    median_mad,
    one_sided_z,
    run_batch,
    validate_batch_body,
)
from app.io import AlignConfig, InputError
from app.main import app

client = TestClient(app)


# --- robust statistics unit tests -------------------------------------------


def test_median_mad_basic_and_empty():
    assert median_mad([1.0, 1.0, 2.0, 2.0, 4.0, 9.0]) == (2.0, 1.0)
    assert median_mad([3.0, 3.0, 3.0]) == (3.0, 0.0)
    assert median_mad([]) == (None, 0.0)
    assert median_mad([None, None]) == (None, 0.0)
    assert median_mad([None, 2.0, 4.0]) == (3.0, 1.0)


def test_one_sided_z_high_side_nonzero_mad():
    # median 2, MAD 1 -> normalised scale 1.4826
    med, mad = median_mad([1.0, 1.0, 2.0, 2.0, 4.0, 9.0])
    assert med == 2.0 and mad == 1.0
    z_bad, bad = one_sided_z(12.0, med, mad, high_side=True)
    assert bad and z_bad == pytest.approx(10.0 / 1.4826)
    # A value on the low side never trips the high-side rule.
    z_ok, not_bad = one_sided_z(0.0, med, mad, high_side=True)
    assert not not_bad and z_ok < 0
    # Just under the threshold.
    _, not_bad2 = one_sided_z(med + 2.0 * 1.4826 * mad, med, mad, True)
    assert not not_bad2


def test_one_sided_z_low_side_nonzero_mad():
    med, mad = median_mad([1.0, 1.0, 2.0, 2.0, 4.0, 9.0])
    assert med == 2.0 and mad == 1.0
    z_bad, bad = one_sided_z(-10.0, med, mad, high_side=False)
    assert bad and z_bad == pytest.approx(12.0 / 1.4826)
    # High-side deviation must not trip the low-side rule.
    _, not_bad = one_sided_z(9.0, med, mad, high_side=False)
    assert not not_bad
    # A mild dip below the median is not anomalous.
    _, not_bad2 = one_sided_z(med - 1.0, med, mad, high_side=False)
    assert not not_bad2


def test_one_sided_z_degenerate_mad_high_side():
    # MAD 0: only values above the median are high-side anomalies.
    z, bad = one_sided_z(1.0, 0.0, 0.0, high_side=True)
    assert bad and z is None
    z_eq, bad_eq = one_sided_z(0.0, 0.0, 0.0, high_side=True)
    assert not bad_eq and z_eq == 0.0
    # Below median is the good side for a high-side metric.
    _, bad_low = one_sided_z(-1.0, 0.0, 0.0, high_side=True)
    assert not bad_low


def test_one_sided_z_degenerate_mad_low_side():
    # MAD 0: only values below the median are low-side anomalies.
    z, bad = one_sided_z(0.8, 1.0, 0.0, high_side=False)
    assert bad and z is None
    _, not_bad = one_sided_z(1.2, 1.0, 0.0, high_side=False)
    assert not not_bad
    _, not_bad_eq = one_sided_z(1.0, 1.0, 0.0, high_side=False)
    assert not not_bad_eq


def test_one_sided_z_none_value():
    z, bad = one_sided_z(None, 1.0, 0.0, high_side=True)
    assert z is None and not bad


# --- request validation ------------------------------------------------------


def _good_body():
    ref = [{"timestamp": 100.0 + 10 * i, "label": "X"} for i in range(10)]
    tgt = [{"timestamp": 105.0 + 10 * i, "label": "X"} for i in range(10)]
    return {
        "reference": ref,
        "targets": [
            {"name": "a", "target": tgt},
            {"name": "b", "target": tgt},
        ],
    }


@pytest.mark.parametrize("n, ok", [(1, False), (2, True), (20, True), (21, False)])
def test_validate_target_count(n, ok):
    body = _good_body()
    body["targets"] = [{"name": f"t{i}", "target": body["targets"][0]["target"]}
                       for i in range(n)]
    if ok:
        _, targets, _ = validate_batch_body(body)
        assert len(targets) == n
    else:
        with pytest.raises(InputError, match="2..20"):
            validate_batch_body(body)


def test_validate_name_rules():
    body = _good_body()
    body["targets"][1]["name"] = "a"
    with pytest.raises(InputError, match="duplicate target name"):
        validate_batch_body(body)

    body = _good_body()
    body["targets"][0]["name"] = "  "
    with pytest.raises(InputError, match="name"):
        validate_batch_body(body)

    body = _good_body()
    del body["targets"][0]["target"]
    with pytest.raises(InputError, match="targets\\[0\\]"):
        validate_batch_body(body)

    with pytest.raises(InputError, match="object"):
        validate_batch_body({"reference": [], "targets": [1, 2]})


def test_validate_shared_params():
    body = _good_body()
    body["iterations"] = 0
    with pytest.raises(InputError, match="iterations"):
        validate_batch_body(body)
    body = _good_body()
    body["time_weight"] = 9.0
    with pytest.raises(InputError, match="time_weight"):
        validate_batch_body(body)
    body = _good_body()
    body["time_field"] = 7
    with pytest.raises(InputError, match="time_field"):
        validate_batch_body(body)


def test_validate_target_parse_error_is_located():
    body = _good_body()
    body["targets"][1]["target"] = [{"nope": 1}]
    with pytest.raises(InputError, match=r"targets\[1\] \(b\)"):
        reference, targets, cfg = validate_batch_body(body)
        run_batch(reference, targets, cfg)


# --- end-to-end batch driver -------------------------------------------------


def make_ref(n=40, step=10.0, base=1_000_000.0):
    labels = ["A", "B", "C", "D", "E"]
    return [
        {"timestamp": base + i * step, "label": labels[i % 5], "id": i}
        for i in range(n)
    ]


def make_good_target(refs, drift_s=5.0):
    return [{"timestamp": r["timestamp"] + drift_s, "label": r["label"],
             "id": r["id"]} for r in refs]


def test_run_batch_all_good_no_anomalies_and_ordering():
    refs = make_ref()
    targets = [
        ("first", make_good_target(refs, 5.0)),
        ("second", make_good_target(refs, 5.001)),
        ("third", make_good_target(refs, 5.0)),
    ]
    body = {
        "reference": refs,
        "targets": [{"name": n, "target": t} for n, t in targets],
    }
    reference, items, cfg = validate_batch_body(body)
    report = run_batch(reference, items, cfg)

    assert report["summary"]["target_count"] == 3
    assert report["summary"]["anomalous_count"] == 0
    assert report["summary"]["z_threshold"] == Z_THRESHOLD
    # No anomalies -> input order preserved (stable).
    assert [r["name"] for r in report["results"]] == ["first", "second", "third"]
    assert [r["rank"] for r in report["results"]] == [1, 2, 3]
    for r in report["results"]:
        assert r["matched_count"] == 40
        assert r["matched_ratio"] == pytest.approx(1.0)
        assert r["residual_rms"] == pytest.approx(0.0, abs=1e-9)
        assert r["z_scores"]["residual_rms"] == 0.0
        assert not r["anomalous"]
        assert r["anomaly_reasons"] == []
    stats = report["statistics"]["residual_rms"]
    assert stats["median"] == pytest.approx(0.0, abs=1e-9) and stats["mad"] == 0.0


def test_run_batch_degraded_target_flagged_and_ranked_first():
    # Three near-identical healthy logs plus one degraded log: half its
    # events carry wrong labels and the rest are heavily jittered, so both
    # matched_ratio and mean_confidence fall well below the batch.
    random.seed(7)
    refs = make_ref()
    degraded = [
        {"timestamp": r["timestamp"] + 5.0 + random.uniform(-1.5, 1.5),
         "label": (f"Z{r['id']}" if r["id"] % 2 else r["label"]),
         "id": r["id"]}
        for r in refs
    ]
    names_logs = [
        ("a", make_good_target(refs)),
        ("b", make_good_target(refs, 5.002)),
        ("c", make_good_target(refs, 4.999)),
        ("bad", degraded),
    ]
    body = {"reference": refs,
            "targets": [{"name": n, "target": t} for n, t in names_logs]}
    reference, items, cfg = validate_batch_body(body)
    report = run_batch(reference, items, cfg)

    results = report["results"]
    assert results[0]["name"] == "bad" and results[0]["rank"] == 1
    assert results[0]["anomalous"] is True
    assert "matched_ratio" in results[0]["anomaly_reasons"]
    assert "mean_confidence" in results[0]["anomaly_reasons"]
    assert report["summary"]["anomalous_count"] == 1
    # Healthy rows retain relative input order among themselves.
    assert [r["name"] for r in results[1:]] == ["a", "b", "c"]
    for r in results[1:]:
        assert r["anomalous"] is False


def test_run_batch_zero_anchors_metrics_are_null_safe():
    # Reference with labels the targets cannot match: residual_rms is None
    # and matched_ratio is 0, but nothing crashes and no spurious anomaly.
    refs = [{"timestamp": 100.0 + i, "label": f"R{i}"} for i in range(6)]
    body = {"reference": refs, "targets": [
        {"name": f"t{k}",
         "target": [{"timestamp": 200.0 + i, "label": f"T{i}"}
                    for i in range(6)]}
        for k in range(3)
    ]}
    reference, items, cfg = validate_batch_body(body)
    report = run_batch(reference, items, cfg)
    for r in report["results"]:
        assert r["residual_rms"] is None
        assert r["matched_count"] == 0
        assert r["matched_ratio"] == 0.0
        assert r["z_scores"]["residual_rms"] is None
        assert not r["anomalous"]
    assert report["statistics"]["residual_rms"]["median"] is None


def test_run_batch_ranking_confidence_desc_then_residual_asc():
    # Within the same anomaly class, higher mean_confidence ranks first;
    # ties there fall back to lower residual_rms, then input index.
    refs = make_ref(n=30)
    # A clean log (confidence 1, residual ~0) and a log with a clock jump
    # (still perfectly fitted at anchors -> residual 0, lower confidence).
    jump = []
    for r in refs:
        off = 5.0 if r["timestamp"] < 1_000_100.0 else 25.0
        jump.append({"timestamp": r["timestamp"] + off,
                     "label": r["label"], "id": r["id"]})
    logs = [
        ("jumpy-input-first", jump),
        ("clean", make_good_target(refs, 6.0)),
    ]
    body = {"reference": refs,
            "targets": [{"name": n, "target": t} for n, t in logs]}
    reference, items, cfg = validate_batch_body(body)
    report = run_batch(reference, items, cfg)
    names = [r["name"] for r in report["results"]]
    assert names == ["clean", "jumpy-input-first"]


def test_score_and_rank_ordering_keys():
    from app.batch import _score_and_rank
    # All rows share the same matched_ratio so ranking is driven by the
    # anomaly flag, confidence and residual keys only.  Residuals have a
    # non-zero MAD; 0.5 is above the median but below the z>3 threshold.
    rows = [
        {"input_index": 0, "name": "null-residual", "mean_confidence": 0.9,
         "residual_rms": None, "matched_ratio": 1.0},
        {"input_index": 1, "name": "high-residual", "mean_confidence": 0.9,
         "residual_rms": 0.3, "matched_ratio": 1.0},
        {"input_index": 2, "name": "low-residual", "mean_confidence": 0.9,
         "residual_rms": 0.05, "matched_ratio": 1.0},
        {"input_index": 3, "name": "mid-residual", "mean_confidence": 0.9,
         "residual_rms": 0.1, "matched_ratio": 1.0},
        {"input_index": 4, "name": "confident", "mean_confidence": 1.0,
         "residual_rms": 0.2, "matched_ratio": 1.0},
    ]
    _score_and_rank(rows)
    names = [r["name"] for r in sorted(rows, key=lambda r: r["rank"])]
    # No anomalies; confidence desc (confident before the 0.9s); then
    # residual asc (low 0.05 < mid 0.1 < 0.2 < high 0.3), null last.
    assert names == [
        "confident", "low-residual", "mid-residual", "high-residual",
        "null-residual",
    ]
    assert all(not r["anomalous"] for r in rows)


def test_run_batch_ranking_ties_keep_input_order():
    # Identical quality metrics -> strict input-order stability.
    refs = make_ref(n=12)
    body = {"reference": refs, "targets": [
        {"name": f"line-{i}", "target": make_good_target(refs, 3.0 + 0.01 * i)}
        for i in range(5)
    ]}
    reference, items, cfg = validate_batch_body(body)
    report = run_batch(reference, items, cfg)
    assert [r["name"] for r in report["results"]] == [
        f"line-{i}" for i in range(5)]
    assert [r["rank"] for r in report["results"]] == [1, 2, 3, 4, 5]


# --- HTTP layer --------------------------------------------------------------


def test_batch_endpoint_json_ok():
    refs = make_ref(n=20)
    r = client.post("/align/batch", json={
        "reference": refs,
        "targets": [
            {"name": "line-a", "target": make_good_target(refs)},
            {"name": "line-b", "target": make_good_target(refs, 7.0)},
        ],
        "iterations": 2,
    })
    assert r.status_code == 200
    body = r.json()
    assert {x["name"] for x in body["results"]} == {"line-a", "line-b"}
    for x in body["results"]:
        assert x["matched_count"] == 20
        assert x["matched_ratio"] == pytest.approx(1.0)
        assert {"residual_rms", "mean_confidence", "matched_ratio"} <= set(
            x["z_scores"])
    assert body["summary"]["anomalous_count"] == 0
    assert body["statistics"]["matched_ratio"]["median"] == pytest.approx(1.0)


def test_batch_endpoint_jsonl_string_targets():
    ref = '{"timestamp": 100.0, "label": "A"}\n{"timestamp": 110.0, "label": "B"}\n'
    tgt = '{"timestamp": 104.0, "label": "A"}\n{"timestamp": 114.0, "label": "B"}\n'
    r = client.post("/align/batch", json={
        "reference": ref,
        "targets": [{"name": "one", "target": tgt},
                    {"name": "two", "target": tgt}],
    })
    assert r.status_code == 200
    assert all(x["matched_count"] == 2 for x in r.json()["results"])


def test_batch_endpoint_unsupported_media_type():
    r = client.post("/align/batch", content=b"x",
                    headers={"content-type": "text/plain"})
    assert r.status_code == 415


def test_batch_endpoint_validation_400s():
    refs = make_ref(n=5)
    good = make_good_target(refs)
    cases = [
        ({}, "reference"),
        ({"reference": refs}, "targets"),
        ({"reference": refs, "targets": [{"name": "a", "target": good}]},
         "2..20"),
        ({"reference": refs, "targets": [
            {"name": "a", "target": good},
            {"name": "a", "target": good}]}, "duplicate"),
        ({"reference": refs, "targets": [
            {"name": "a", "target": good},
            {"name": "b", "target": [{"x": 1}]}]}, "targets[1]"),
        ({"reference": [{"x": 1}], "targets": [
            {"name": "a", "target": good},
            {"name": "b", "target": good}]}, "reference"),
        ({"reference": refs, "targets": [
            {"name": "a", "target": good},
            {"name": "b", "target": good}], "time_weight": 5}, "time_weight"),
    ]
    for payload, fragment in cases:
        r = client.post("/align/batch", json=payload)
        assert r.status_code == 400, (payload, fragment, r.status_code)
        assert fragment in r.json()["detail"], (fragment, r.json()["detail"])


def test_batch_endpoint_limit_is_20():
    refs = make_ref(n=5)
    r = client.post("/align/batch", json={
        "reference": refs,
        "targets": [{"name": f"t{i}", "target": make_good_target(refs)}
                    for i in range(MAX_TARGETS + 1)],
    })
    assert r.status_code == 400 and "2..20" in r.json()["detail"]


def test_batch_endpoint_per_target_anchors():
    refs = make_ref(n=10)
    tgt = make_good_target(refs)
    r = client.post("/align/batch", json={
        "reference": refs,
        "targets": [{
            "name": "locked", "target": tgt,
            "anchors": [{"reference_index": 0, "target_index": 0},
                        {"reference_index": 9, "target_index": 9}],
        }, {"name": "free", "target": tgt}],
    })
    assert r.status_code == 200
    rows = {x["name"]: x for x in r.json()["results"]}
    assert rows["locked"]["matched_count"] == 10
    assert rows["free"]["matched_count"] == 10


def test_batch_endpoint_per_target_anchor_error_is_located():
    refs = make_ref(n=10)
    r = client.post("/align/batch", json={
        "reference": refs,
        "targets": [
            {"name": "ok", "target": make_good_target(refs)},
            {"name": "broken", "target": make_good_target(refs),
             "anchors": [{"reference_index": 99, "target_index": 0}]},
        ],
    })
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "targets[1] (broken)" in detail
    assert "reference_index 99 out of range" in detail


def test_align_endpoint_remains_compatible():
    refs = make_ref(n=10)
    r = client.post("/align", json={"reference": refs,
                                    "target": make_good_target(refs)})
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["matched_count"] == 10
    assert "elapsed_seconds" in body["summary"]
