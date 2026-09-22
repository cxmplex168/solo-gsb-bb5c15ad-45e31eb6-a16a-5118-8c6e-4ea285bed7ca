"""FastAPI application: industrial event-log clock calibration.

Endpoints
---------
* ``GET  /health``       -- liveness probe
* ``POST /align``        -- calibrate a drifted target JSONL against a reference
* ``POST /align/batch``  -- calibrate 2..20 named targets against one reference
                            and summarise their alignment quality

The ``/align`` endpoint accepts two equivalent content types:

* ``application/json`` -- body
  ``{"reference": [...], "target": [...], "time_field": ..., ...}``;
  ``reference`` / ``target`` may also be raw JSONL strings.
* ``multipart/form-data`` -- ``reference`` and ``target`` uploaded as
  ``.jsonl`` files plus optional form fields (``time_field`` ...).
"""

from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .batch import assess_batch, parse_batch_request
from .io import AlignConfig, InputError, run_alignment

app = FastAPI(
    title="Industrial Event-Log Clock Alignment API",
    version=__version__,
    description=(
        "Aligns a clock-drifting event log (JSONL) against a reference log "
        "with monotonic label/DP matching and a piecewise-linear clock map."
    ),
)


# --- request / response schemas (OpenAPI) -----------------------------------


class AnchorItem(BaseModel):
    reference_index: int = Field(
        ..., ge=0, description="0-based index of the locked reference event"
    )
    target_index: int = Field(
        ..., ge=0, description="0-based index of the locked target event"
    )


class AlignRequest(BaseModel):
    reference: Any = Field(
        ..., description="Reference log: JSON array of events or JSONL string"
    )
    target: Any = Field(
        ..., description="Log to calibrate: JSON array of events or JSONL string"
    )
    anchors: Optional[list[AnchorItem]] = Field(
        default=None,
        description=(
            "Manual (non-skippable) anchor locks as "
            "{reference_index, target_index} pairs. The order-preserving "
            "matcher runs independently inside each gap between locks and "
            "may neither reuse a locked event nor cross an anchor."
        ),
    )
    time_field: Optional[str] = Field(
        default=None,
        description="Timestamp field name (auto-detected if omitted)",
    )
    label_field: Optional[str] = Field(
        default=None,
        description="Event tag field name (auto-detected if omitted)",
    )
    iterations: int = Field(
        default=3, ge=1, le=10,
        description="Match/fit refinement iterations",
    )
    time_weight: float = Field(
        default=0.5, ge=0.0, le=2.0,
        description="Weight of the temporal Gaussian term vs. the label bonus",
    )


def _to_config(
    time_field: Optional[str],
    label_field: Optional[str],
    iterations: int,
    time_weight: float,
    anchors: Any = None,
) -> AlignConfig:
    return AlignConfig(
        time_field=time_field or None,
        label_field=label_field or None,
        iterations=iterations,
        time_weight=time_weight,
        manual_anchors=anchors,
    )


# --- endpoints --------------------------------------------------------------


@app.get("/health", tags=["ops"])
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "clock-align", "version": __version__}


@app.post("/align", tags=["alignment"], response_model=None)
async def align(request: Request) -> JSONResponse:
    """Calibrate the target log. Works with JSON or multipart/form-data."""
    content_type = request.headers.get("content-type", "")

    try:
        if content_type.startswith("application/json"):
            try:
                body = await request.json()
            except Exception as exc:  # noqa: BLE001
                raise InputError(f"invalid JSON body: {exc}") from exc
            if not isinstance(body, dict):
                raise InputError(
                    "JSON body must be an object with 'reference' and 'target'"
                )
            if "reference" not in body or "target" not in body:
                raise InputError("body requires 'reference' and 'target'")
            cfg = _to_config(
                body.get("time_field"),
                body.get("label_field"),
                int(body.get("iterations", 3)),
                float(body.get("time_weight", 0.5)),
                body.get("anchors"),
            )
            reference, target = body["reference"], body["target"]

        elif content_type.startswith("multipart/form-data"):
            form = await request.form()
            if "reference" not in form or "target" not in form:
                raise InputError(
                    "form requires 'reference' and 'target' file parts"
                )
            reference_file: UploadFile = form["reference"]
            target_file: UploadFile = form["target"]
            reference = await reference_file.read()
            target = await target_file.read()
            cfg = _to_config(
                form.get("time_field"),
                form.get("label_field"),
                int(form.get("iterations", 3)),
                float(form.get("time_weight", 0.5)),
                # JSON string, e.g.
                # -F 'anchors=[{"reference_index":0,"target_index":0}]'
                form.get("anchors"),
            )
        else:
            raise HTTPException(
                status_code=415,
                detail=(
                    "unsupported content type; use application/json or "
                    "multipart/form-data"
                ),
            )

        started = time.perf_counter()
        result = run_alignment(reference, target, cfg)
        result["summary"]["elapsed_seconds"] = round(
            time.perf_counter() - started, 6
        )
        return JSONResponse(result)

    except InputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


_BATCH_REQUEST_EXAMPLE = {
    "reference": [
        {"timestamp": 1790000000.0, "label": "LINE_START"},
        {"timestamp": 1790000030.0, "label": "HEATER_ON"},
        {"timestamp": 1790000060.0, "label": "TEMP_RAMP"},
    ],
    "targets": [
        {
            "name": "line-A",
            "target": [
                {"timestamp": 1790000005.6, "label": "LINE_START"},
                {"timestamp": 1790000036.2, "label": "HEATER_ON"},
                {"timestamp": 1790000067.4, "label": "TEMP_RAMP"},
            ],
        },
        {
            "name": "line-B",
            "target": [
                {"timestamp": 1790000006.0, "label": "LINE_START"},
                {"timestamp": 1790000037.0, "label": "HEATER_ON"},
                {"timestamp": 1790000040.0, "label": "GHOST"},
                {"timestamp": 1790000068.0, "label": "TEMP_RAMP"},
            ],
        },
    ],
    "iterations": 3,
    "time_weight": 0.5,
}


@app.post("/align/batch", tags=["alignment"], response_model=None,
          openapi_extra={
              "summary": "Batch quality assessment of 2..20 target logs",
              "requestBody": {
                  "required": True,
                  "content": {
                      "application/json": {
                          "examples": {
                              "batch": {"summary": "one reference, two targets",
                                        "value": _BATCH_REQUEST_EXAMPLE}
                          }
                      }
                  },
              },
          })
async def align_batch(request: Request) -> JSONResponse:
    """Calibrate several named targets against one reference.

    JSON body only: ``{"reference": ..., "targets": [{"name": ...,
    "target": ..., "anchors"?: ...}, ...], ...}``.  Each target runs the
    same pipeline as ``/align``; the response ranks them by robust
    median/MAD z-scores of ``residual_rms`` (high side) and
    ``mean_confidence`` / ``matched_ratio`` (low side); any |z| > 3 flags
    the target as anomalous.
    """
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("application/json"):
        raise HTTPException(
            status_code=415,
            detail="/align/batch accepts application/json only",
        )

    try:
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise InputError(f"invalid JSON body: {exc}") from exc

        reference, targets, cfg = parse_batch_request(body)
        started = time.perf_counter()
        result = assess_batch(reference, targets, cfg)
        result["summary"]["elapsed_seconds"] = round(
            time.perf_counter() - started, 6
        )
        return JSONResponse(result)

    except InputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
