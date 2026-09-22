#!/usr/bin/env python3
"""Generate example data for POST /align/batch.

Writes, under ``examples/batch/``:

* ``reference.jsonl``        -- one shared trusted reference log;
* ``target-line-1.jsonl`` .. ``target-line-5.jsonl`` -- five named target
  logs (four healthy lines with small clock drift/jitter, one "line-5"
  whose event labels are shuffled so it barely matches the reference --
  a clear quality outlier for the batch endpoint);
* ``batch_request.json``     -- ready-to-send JSON request body in which
  the JSONL logs are embedded as strings.

Run:  python scripts/make_batch_examples.py
"""

from __future__ import annotations

import json
import os
import random

LABELS = [
    "LINE_START", "HEATER_ON", "TEMP_RAMP", "VALVE_OPEN",
    "PRESSURE_OK", "WELD_BEGIN", "WELD_END", "COOLING",
    "QC_SNAPSHOT", "LINE_STOP",
]

T0 = 1_790_000_000.0
N_EVENTS = 40


def line_time(ref_time: float, offset: float) -> float:
    """Target-clock reading for a healthy line: fixed offset + slow drift."""
    return ref_time + offset + 0.01 * (ref_time - T0)


def main() -> None:
    random.seed(7)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(here, "examples", "batch")
    os.makedirs(out_dir, exist_ok=True)

    refs = [
        {"timestamp": round(T0 + 15.0 * k, 3), "label": LABELS[k % len(LABELS)],
         "line": 0, "seq": k}
        for k in range(N_EVENTS)
    ]

    # Four healthy lines: distinct fixed offsets, small timestamp jitter.
    offsets = {"line-1": 2.5, "line-2": -4.0, "line-3": 8.0, "line-4": 1.25}
    targets: dict[str, list[dict]] = {}
    for name, offset in offsets.items():
        targets[name] = [
            {
                "timestamp": round(
                    line_time(rec["timestamp"], offset)
                    + random.uniform(-0.02, 0.02), 3),
                "label": rec["label"],
                "line": name,
                "seq": rec["seq"],
            }
            for rec in refs
        ]

    # One unhealthy line: timestamps look plausible, but labels are rotated
    # so the order-preserving matcher finds almost no consistent pairs.
    bad = [
        {
            "timestamp": round(line_time(rec["timestamp"], 6.0)
                               + random.uniform(-0.02, 0.02), 3),
            "label": LABELS[(rec["seq"] + 3) % len(LABELS)],
            "line": "line-5",
            "seq": rec["seq"],
        }
        for rec in refs
    ]
    targets["line-5"] = bad

    def write_jsonl(filename: str, records: list[dict]) -> str:
        path = os.path.join(out_dir, filename)
        with open(path, "w") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")
        return path

    write_jsonl("reference.jsonl", refs)
    for name, records in targets.items():
        write_jsonl(f"target-{name}.jsonl", records)

    # JSONL content embedded as strings, exactly as /align accepts inline.
    request_body = {
        "reference": "\n".join(json.dumps(r) for r in refs) + "\n",
        "targets": [
            {
                "name": name,
                "target": "\n".join(json.dumps(r) for r in records) + "\n",
            }
            for name, records in targets.items()
        ],
        "iterations": 3,
        "time_weight": 0.5,
    }
    with open(os.path.join(out_dir, "batch_request.json"), "w") as f:
        json.dump(request_body, f, indent=2)
        f.write("\n")

    print(f"wrote 1 reference and {len(targets)} target logs to {out_dir}")
    print("line-5 carries shuffled labels and is expected to be flagged")


if __name__ == "__main__":
    main()
