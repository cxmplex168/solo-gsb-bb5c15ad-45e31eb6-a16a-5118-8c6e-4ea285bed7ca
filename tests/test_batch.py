"""Tests for the batch quality-assessment endpoint (POST /align/batch)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.batch import (
    assess_batch,
    median_mad,
    one_sided_z,
    parse_batch_request,
)
from app.io import AlignConfig, InputError
from app.main import app

client = TestClient(app)


# --- robust statistics ------------------------------------------------------


def test_median_mad():
    assert median_mad([1.0, 2.0, 3.0]) == (2.0, 1.0)
    assert median_mad([5.0]) == (5.0, 0.0)
    assert median_mad([]) == (None, None)
    assert median_mad([None, None]) == (None, None)
    # Nones are ignored, not treated as zeros.
    assert median_mad([None, 2.0, 4.0]) == (3.0, 1.0)


def test_one_sided_z_high_side():
    # z = 0.6745 * (x - med) / MAD.
    z, bad = one_sided_z(7.0, 3.0, 1.0, high_side=True)
    assert z == pytest.approx(0.6745 * 4.0)
    assert bad is False
    # Below the median on the high side is clamped to 0, never flagged.
    z, bad = one_sided_z(-100.0, 3.0, 1.0, high_side=True)
    assert (z, bad) == (0.0, False)
    # Missing metric never flags.
    assert one_sided_z(None, 3.0, 1.0, high_side=True) == (None, False)


def test_one_sided_z_low_side():
    z, bad = one_sided_z(1.0, 5.0, 1.0, high_side=False)
    assert z == pytest.approx(-0.6745 * 4.0)
    assert bad is False
    z, bad = one_sided_z(0.0, 5.0, 1.0, high_side=False)
    assert bad is True and z < -3.0
    # Above the median on the low side is clamped to 0, never flagged.
    z, bad = one_sided_z(100.0, 5.0, 1.0, high_side=False)
    assert (z, bad) == (0.0, False)


def test_one_sided_z_degenerate_mad():
    # MAD == 0: only a value differing from the median on the scored side is
    # flagged; z is then null (no finite scale).
    assert one_sided_z(3.0, 3.0, 0.0, high_side=True) == (0.0, False)
    z, bad = one_sided_z(3.001, 3.0, 0.0, high_side=True)
    assert (z, bad) == (None, True)
    assert one_sided_z(3.0, 3.0, 0.0, high_side=False) == (0.0, False)
    z, bad = one_sided_z(2.999, 3.0, 0.0, high_side=False)
    assert (z, bad) == (None, True)
    # Difference on the *wrong* side still clamps to zero.
    assert one_sided_z(2.999, 3.0, 0.0, high_side=True) == (0.0, False)
    assert one_sided_z(3.001, 3.0, 0.0, high_side=False) == (0.0, False)


# --- request validation -----------------------------------------------------


def _simple_body(targets):
    refs = [
        {"timestamp": 100.0, "label": "A"},
        {"timestamp": 110.0, "label": "B"},
        {"timestamp": 120.0, "label": "C"},
    ]
    return {"reference": refs, "targets": targets}


def test_parse_batch_request_ok():
    body = _simple_body([
        {"name": "a", "target": [{"timestamp": 101.0, "label": "A"}]},
        {"name": "b", "target": [{"timestamp": 102.0, "label": "A"}],
         "anchors": [{"reference_index": 0, "target_index": 0}]},
    ])
    ref, targets, cfg = parse_batch_request(body)
    assert cfg.iterations == 3 and cfg.time_weight == 0.5
    assert [t["name"] for t in targets] == ["a", "b"]
    assert targets[1]["anchors"][0]["reference_index"] == 0
    assert ref is body["reference"]


@pytest.mark.parametrize("body, fragment", [
    (["not", "an", "object"], "must be an object"),
    ({"targets": []}, "'reference'"),
    ({"reference": []}, "'targets'"),
    ({"reference": [], "targets": []}, "2..20"),
    ({"reference": [], "targets": [1, 2]}, "target entry must be an object"),
    ({"reference": [], "targets": [{"target": []},
                                  {"name": "b", "target": []}]}, "'name'"),
    ({"reference": [], "targets": [{"name": "", "target": []},
                                  {"name": "b", "target": []}]},
     "non-empty string"),
    ({"reference": [], "targets": [{"name": "x", "target": []},
                                   {"name": "y", "target": []},
                                   {"name": "x", "target": []}]},
     "duplicate target name"),
    ({"reference": [], "targets": [{"name": "x"},
                                  {"name": "y", "target": []}]},
     "missing required field"),
    ({"reference": [], "targets": [{"name": "a", "target": []},
                                   {"name": "b", "target": []}],
      "iterations": 0}, "iterations"),
    ({"reference": [], "targets": [{"name": "a", "target": []},
                                   {"name": "b", "target": []}],
      "time_weight": 5.0}, "time_weight"),
    ({"reference": [], "targets": [{"name": "a", "target": []},
                                   {"name": "b", "target": []}],
      "iterations": "3"}, "iterations"),
    ({"reference": [], "targets": [{"name": "a", "target": []},
                                   {"name": "b", "target": []}],
      "time_weight": float("nan")}, "time_weight"),
])
def test_parse_batch_request_errors(body, fragment):
    with pytest.raises(InputError) as exc:
        parse_batch_request(body)
    assert fragment in str(exc.value)


def test_targets_limit_is_20():
    entries = [{"name": f"t{i}", "target": []} for i in range(21)]
    with pytest.raises(InputError, match="2..20"):
        parse_batch_request({"reference": [], "targets": entries})
    # Exactly 20 passes structural validation.
    entries = entries[:20]
    _, targets, _ = parse_batch_request(
        {"reference": [], "targets": entries})
    assert len(targets) == 20


# --- end-to-end engine assessment -------------------------------------------


def make_ref(n=30, step=10.0, base=1_000_000.0):
    labels = ["A", "B", "C", "D", "E"]
    return [
        {"timestamp": base + i * step, "label": labels[i % 5], "id": i}
        for i in range(n)
    ]


def drift(t):
    if t < 1_000_200.0:
        return t + 5.0 + 0.02 * (t - 1_000_000.0)
    return t + 65.0 + 0.01 * (t - 1_000_000.0)


def good_target(refs, jitter=0.01):
    return [
        {"timestamp": drift(r["timestamp"]) + jitter * (r["id"] % 3),
         "label": r["label"]}
        for r in refs
    ]


def unmatchable_target(refs):
    # Every label is unique and disjoint from the reference vocabulary.
    return [
        {"timestamp": drift(r["timestamp"]), "label": f"Z{r['id']}"}
        for r in refs
    ]


def test_assess_batch_ranks_and_flags_unmatchable():
    refs = make_ref()
    _, targets, cfg = parse_batch_request({
        "reference": refs,
        "targets": [
            {"name": "good-1", "target": good_target(refs)},
            {"name": "bad", "target": unmatchable_target(refs)},
            {"name": "good-2", "target": good_target(refs)},
        ],
    })
    out = assess_batch(refs, targets, cfg)

    assert out["summary"]["target_count"] == 3
    assert out["summary"]["anomaly_count"] == 1
    assert out["summary"]["z_threshold"] == 3.0

    by_name = {r["name"]: r for r in out["results"]}
    bad = by_name["bad"]
    assert bad["rank"] == 1 and bad["anomalous"] is True
    assert "mean_confidence_low" in bad["anomaly_reasons"]
    assert "matched_ratio_low" in bad["anomaly_reasons"]
    assert bad["matched_count"] == 0
    assert bad["matched_ratio"] == 0.0
    assert bad["residual_rms"] is None
    assert bad["z_residual_rms"] is None

    # Non-anomalous targets follow, ordered by mean_confidence desc, and the
    # input order breaks the tie (good-1 at input_index 0 before good-2 at 2).
    assert [r["name"] for r in out["results"]] == ["bad", "good-1", "good-2"]
    g1, g2 = by_name["good-1"], by_name["good-2"]
    assert g1["rank"] == 2 and g2["rank"] == 3
    assert g1["input_index"] == 0 and g2["input_index"] == 2
    assert g1["matched_ratio"] == 1.0 and g1["anomalous"] is False


def test_assess_batch_residual_rms_high_outlier():
    # Simulate the metric layer directly: residual_rms high-side robust z.
    from app.batch import _annotate_outliers
    rms_values = [0.010, 0.012, 0.011, 0.009, 5.0]
    rows = [
        {"name": f"t{i}", "residual_rms": rms_values[i],
         "mean_confidence": 0.9, "matched_count": 10, "target_count": 10,
         "matched_ratio": 1.0, "_order": i}
        for i in range(5)
    ]
    _annotate_outliers(rows)
    bad = rows[4]
    assert bad["anomalous"] is True
    assert bad["anomaly_reasons"] == ["residual_rms_high"]
    assert bad["z_residual_rms"] > 3.0
    for row in rows[:4]:
        assert row["anomalous"] is False


def test_assess_batch_degenerate_mad_flags_different_value():
    from app.batch import _annotate_outliers
    rows = []
    for i, conf in enumerate([0.9, 0.9, 0.9, 0.5]):
        rows.append({
            "name": f"t{i}", "residual_rms": 0.0,
            "mean_confidence": conf, "matched_count": 10,
            "target_count": 10, "matched_ratio": 1.0, "_order": i,
        })
    _annotate_outliers(rows)
    assert rows[3]["anomalous"] is True
    assert rows[3]["z_mean_confidence"] is None
    assert rows[3]["anomaly_reasons"] == ["mean_confidence_low"]
    assert all(not r["anomalous"] for r in rows[:3])


def test_assess_batch_target_error_is_located():
    refs = make_ref()
    body = {
        "reference": refs,
        "targets": [
            {"name": "ok", "target": good_target(refs)},
            {"name": "broken", "target": [{"nope": 1}]},
        ],
    }
    _, targets, cfg = parse_batch_request(body)
    with pytest.raises(InputError) as exc:
        assess_batch(refs, targets, cfg)
    msg = str(exc.value)
    assert "targets[1]" in msg and "'broken'" in msg


def test_assess_batch_per_target_anchors():
    refs = make_ref(n=20)
    body = {
        "reference": refs,
        "targets": [
            {"name": "locked", "target": good_target(make_ref(n=20)),
             "anchors": [{"reference_index": 0, "target_index": 0},
                         {"reference_index": 19, "target_index": 19}]},
            {"name": "free", "target": good_target(make_ref(n=20))},
        ],
    }
    _, targets, cfg = parse_batch_request(body)
    out = assess_batch(refs, targets, cfg)
    locked = next(r for r in out["results"] if r["name"] == "locked")
    assert locked["manual_anchor_count"] == 2


# --- HTTP layer -------------------------------------------------------------


def test_batch_endpoint_json():
    refs = make_ref()
    body = {
        "reference": refs,
        "targets": [
            {"name": "good-1", "target": good_target(refs)},
            {"name": "bad", "target": unmatchable_target(refs)},
            {"name": "good-2", "target": good_target(refs)},
        ],
    }
    r = client.post("/align/batch", json=body)
    assert r.status_code == 200
    out = r.json()
    assert out["results"][0]["name"] == "bad"
    assert out["results"][0]["anomalous"] is True
    assert "elapsed_seconds" in out["summary"]
    # Batch statistics for every metric are reported.
    for key in ("residual_rms", "mean_confidence", "matched_ratio"):
        assert "median" in out["summary"]["statistics"][key]
        assert "mad" in out["summary"]["statistics"][key]


def test_batch_endpoint_accepts_jsonl_strings():
    refs = '\n'.join(
        '{"timestamp": %f, "label": "A"}' % (100.0 + 10 * i)
        for i in range(6))
    tgt = '\n'.join(
        '{"timestamp": %f, "label": "A"}' % (105.0 + 10 * i)
        for i in range(6))
    r = client.post("/align/batch", json={
        "reference": refs,
        "targets": [{"name": "x", "target": tgt},
                    {"name": "y", "target": tgt}],
    })
    assert r.status_code == 200
    for row in r.json()["results"]:
        assert row["matched_count"] == 6 and row["matched_ratio"] == 1.0


@pytest.mark.parametrize("body, fragment", [
    ({"targets": [{"name": "a", "target": []},
                  {"name": "b", "target": []}]}, "'reference'"),
    ({"reference": [], "targets": [{"name": "only", "target": []}]},
     "2..20"),
    ({"reference": [], "targets": [{"name": "a", "target": []},
                                   {"name": "a", "target": []}]},
     "duplicate"),
])
def test_batch_endpoint_validation_400(body, fragment):
    r = client.post("/align/batch", json=body)
    assert r.status_code == 400
    assert fragment in r.json()["detail"]


def test_batch_endpoint_bad_json_400():
    r = client.post("/align/batch", content="not-json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert "invalid JSON" in r.json()["detail"]


def test_batch_endpoint_unsupported_media_type():
    r = client.post("/align/batch", content="abc",
                    headers={"content-type": "text/plain"})
    assert r.status_code == 415


def test_batch_endpoint_target_parse_error_400():
    refs = make_ref(n=8)
    r = client.post("/align/batch", json={
        "reference": refs,
        "targets": [
            {"name": "ok", "target": good_target(refs)},
            {"name": "broken", "target": [{"label": "A"}]},
        ],
    })
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "targets[1]" in detail and "broken" in detail


def test_single_align_endpoint_still_works():
    """The /align contract must remain fully compatible."""
    refs = make_ref(n=12)
    r = client.post("/align", json={
        "reference": refs, "target": good_target(refs)})
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["matched_count"] == 12
    assert set(body) >= {"summary", "clock_mapping", "anchors",
                         "aligned_events", "missing_reference_events"}


def test_openapi_documents_both_endpoints():
    schema = client.get("/openapi.json").json()
    assert "/align" in schema["paths"]
    assert "/align/batch" in schema["paths"]
