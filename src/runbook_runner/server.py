from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from src.runbook_runner.engine import RunbookError, RunbookExecutor, verify_signature

app = FastAPI(title="day0-day2-runbook-runner", version="0.1.0")


def _executor() -> RunbookExecutor:
    repo_root = Path(__file__).resolve().parents[2]
    return RunbookExecutor(
        runbooks_dir=repo_root / "runbooks",
        region=os.getenv("AWS_REGION", "us-east-1"),
        endpoint_url=os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566"),
        bucket=os.getenv("RUNBOOK_BUCKET", "day0-day2-runbook-logs"),
    )


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    repo_root = Path(__file__).resolve().parents[2]
    loaded = sorted(load_ids(repo_root / "runbooks"))
    return {
        "status": "ok",
        "runbooks_loaded": loaded,
        "utc_time": datetime.now(timezone.utc).isoformat(),
    }


def load_ids(runbooks_dir: Path) -> list[str]:
    from src.runbook_runner.engine import load_runbooks

    return list(load_runbooks(runbooks_dir).keys())


@app.post("/v1/runbooks/run")
async def run_runbook(
    request: Request,
    x_signature_256: str | None = Header(default=None, alias="X-Signature-256"),
    x_timestamp: str | None = Header(default=None, alias="X-Timestamp"),
) -> dict[str, Any]:
    raw_body = await request.body()
    secret = os.getenv("WEBHOOK_HMAC_SECRET", "local-dev-runbook-secret")
    ok, reason = verify_signature(secret, x_signature_256, x_timestamp, raw_body)
    if not ok:
        raise HTTPException(status_code=401, detail={"error": reason})

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail={"error": "malformed json payload"})

    rb_id = payload.get("runbook_id", "")
    incident_id = payload.get("incident_id")
    grace_ext = bool(payload.get("grace_extension_active", False))

    try:
        res = _executor().execute(
            runbook_id=rb_id,
            incident_id=incident_id,
            grace_extension_active=grace_ext,
        )
    except RunbookError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)})
    except Exception as exc:
        raise HTTPException(status_code=502, detail={"error": f"execution failed: {exc}"})

    return {"status": "completed", "result": res}
