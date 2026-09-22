"""FastAPI application: industrial event-log clock calibration.

Endpoints
---------
* ``GET   /health``       -- liveness probe
* ``POST  /align``        -- calibrate one drifted target JSONL against a
  reference log
* ``POST  /align/batch``  -- run the same alignment for 2..20 named target
  logs against one reference and return a batch quality-assessment report
  (robust median/MAD z-scores, anomaly flags, ranked results)

The ``/align`` endpoint accepts two equivalent content types:

* ``application/json`` -- body
  ``{"reference": [...], "target": [...], "time_field": ..., ...}``;
  ``reference`` / ``target`` may also be raw JSONL strings.
* ``multipart/form-data`` -- ``reference`` and ``target`` uploaded as
  ``.jsonl`` files plus optional form fields (``time_field`` ...).

``/align/batch`` is JSON only: ``{"reference": ..., "targets":
[{"name": "line-a", "target": ...}, ...], ...}`` (2..20 targets).
"""

from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .batch import run_batch, validate_batch_body
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


class BatchTargetItem(BaseModel):
    name: str = Field(
        ..., min_length=1,
        description="Unique label of this target log (echoed in the report)",
    )
    target: Any = Field(
        ...,
        description="Log to calibrate: JSON array of events or JSONL string",
    )
    anchors: Optional[list[AnchorItem]] = Field(
        default=None,
        description="Optional per-target manual anchor locks",
    )


class BatchAlignRequest(BaseModel):
    reference: Any = Field(
        ..., description="Reference log: JSON array of events or JSONL string"
    )
    targets: list[BatchTargetItem] = Field(
        ..., min_length=2, max_length=20,
        description="2..20 named target logs assessed against the reference",
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
        description="Match/fit refinement iterations (shared by all targets)",
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


_BATCH_REQUEST_SCHEMA = BatchAlignRequest.model_json_schema()


def _inline_defs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve ``$defs``/``$ref`` in a pydantic schema into one document.

    Keeps the manually-parsed endpoint out of components while still giving
    Swagger UI a fully self-contained request-body schema.
    """
    defs = schema.pop("$defs", {})

    def resolve(node: Any, seen: frozenset[str] = frozenset()) -> Any:
        if isinstance(node, list):
            return [resolve(v, seen) for v in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            key = node["$ref"].split("/")[-1]
            if key in seen:          # defensive against recursive schemas
                return {}
            return resolve(dict(defs[key]), seen | {key})
        return {k: resolve(v, seen) for k, v in node.items()}

    return resolve(schema)


_BATCH_REQUEST_SCHEMA = _inline_defs(_BATCH_REQUEST_SCHEMA)


@app.post(
    "/align/batch",
    tags=["alignment"],
    response_model=None,
    openapi_extra={"requestBody": {
        "required": True,
        "content": {"application/json": {"schema": _BATCH_REQUEST_SCHEMA}},
    }},
)
async def align_batch(request: Request) -> JSONResponse:
    """Assess 2..20 named target logs against one reference in a single call.

    Each target reuses the exact ``/align`` pipeline; the response reports
    per-target ``residual_rms``, ``mean_confidence`` and ``matched_ratio``
    with batch-relative one-sided robust (median/MAD) z-scores, anomaly
    flags and a ranked result list.
    """
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("application/json"):
        raise HTTPException(
            status_code=415,
            detail=(
                "unsupported content type; /align/batch accepts "
                "application/json only"
            ),
        )
    try:
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise InputError(f"invalid JSON body: {exc}") from exc
        reference, targets, cfg = validate_batch_body(body)
        result = run_batch(reference, targets, cfg)
        return JSONResponse(result)
    except InputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
