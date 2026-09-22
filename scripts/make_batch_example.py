#!/usr/bin/env python3
"""Generate an example POST /align/batch request payload.

One reference log and 5 named target logs with deliberately different
quality levels are written to ``examples/batch_request.json``:

* ``line-a`` / ``line-b`` / ``line-c`` -- healthy: a constant clock
  offset, every event matched, anchor confidence at 1.0 (batch medians);
* ``line-d`` -- every third event has a 15 s local time glitch: all
  events still match (the monotone DP skips the bad candidate windows),
  but the leave-one-out residuals around the glitches lower
  ``mean_confidence`` (low-side outlier);
* ``line-e`` -- every second event was relabelled by the source system:
  half the events cannot be matched, so ``matched_ratio`` drops to 0.5
  (low-side outlier).

The batch median/MAD z-scores therefore flag ``line-d`` and ``line-e``
and the ranked response lists them first.

Run:  python scripts/make_batch_example.py
Then:
    curl -s http://localhost:8000/align/batch \
        -H 'Content-Type: application/json' \
        --data @examples/batch_request.json | python -m json.tool
"""

from __future__ import annotations

import json
import os

T0 = 1_790_000_000.0
N_EVENTS = 60
STEP = 30.0
OFFSET = 5.0
GLITCH_SECONDS = 15.0


def main() -> None:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(here, "examples")

    reference = [
        {
            "timestamp": round(T0 + STEP * k, 3),
            "label": f"EVT_{k:03d}",
            "line": "ref",
            "seq": k,
        }
        for k in range(N_EVENTS)
    ]

    def healthy(offset: float) -> list[dict]:
        return [
            {
                "timestamp": round(rec["timestamp"] + offset, 3),
                "label": rec["label"],
                "seq": rec["seq"],
            }
            for rec in reference
        ]

    # line-d: a local 15 s glitch on every third event.  The matcher still
    # finds all events (the glitched candidates fall outside the temporal
    # window, so the surrounding anchors bridge the gap), but the local
    # leave-one-out line prediction is disturbed, lowering confidence.
    glitched = [
        {
            "timestamp": round(
                rec["timestamp"] + OFFSET
                + (GLITCH_SECONDS if rec["seq"] % 3 == 0 else 0.0),
                3,
            ),
            "label": rec["label"],
            "seq": rec["seq"],
        }
        for rec in reference
    ]

    # line-e: every other event was relabelled upstream -> unmatchable.
    relabelled = [
        {
            "timestamp": round(rec["timestamp"] + OFFSET, 3),
            "label": (
                rec["label"] if rec["seq"] % 2 == 0
                else f"UNKNOWN_{rec['seq']:03d}"
            ),
            "seq": rec["seq"],
        }
        for rec in reference
    ]

    payload = {
        "iterations": 3,
        "time_weight": 0.5,
        "reference": reference,
        "targets": [
            {"name": "line-a", "target": healthy(5.0)},
            {"name": "line-b", "target": healthy(5.01)},
            {"name": "line-c", "target": healthy(4.99)},
            {"name": "line-d", "target": glitched},
            {"name": "line-e", "target": relabelled},
        ],
    }

    path = os.path.join(out_dir, "batch_request.json")
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    print(f"wrote batch request with 1 reference and 5 targets to {path}")


if __name__ == "__main__":
    main()
