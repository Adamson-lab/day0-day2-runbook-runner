from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
from pathlib import Path
import time
from typing import Any

logger = logging.getLogger("runbook_runner.engine")


class RunbookError(Exception):
    pass


def sign_payload(secret: str, timestamp_unix: int, body: bytes) -> str:
    payload = f"{timestamp_unix}.".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(
    secret: str,
    signature: str | None,
    timestamp: str | None,
    body: bytes,
    max_skew_sec: int = 300,
    now_unix: int | None = None,
) -> tuple[bool, str]:
    if not signature or not timestamp:
        return False, "missing X-Signature-256 or X-Timestamp header"
    try:
        ts = int(timestamp.strip())
    except ValueError:
        return False, "invalid X-Timestamp header"

    # Use UTC epoch seconds so local Daylight Saving Time shifts never alter replay checks.
    now = int(time.time()) if now_unix is None else now_unix
    if abs(now - ts) > max_skew_sec:
        return False, "request timestamp outside replay window"

    want = sign_payload(secret, ts, body)
    if not hmac.compare_digest(want, signature.strip()):
        return False, "invalid hmac signature"
    return True, "ok"


def _is_transient_aws_error(exc: Any) -> bool:
    resp = getattr(exc, "response", {}) or {}
    code = resp.get("Error", {}).get("Code", "")
    status = resp.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
    return code in {
        "Throttling",
        "ThrottlingException",
        "RequestLimitExceeded",
        "TooManyRequestsException",
    } or status in {429, 502, 503, 504}


def _call_aws_with_retry(op: str, fn: Any, exc_cls: Any, attempts: int = 4) -> Any:
    backoff = 0.1
    for idx in range(1, attempts + 1):
        try:
            return fn()
        except exc_cls as exc:
            if not _is_transient_aws_error(exc) or idx == attempts:
                raise
            # Cap per-retry pause at 2.0s so synchronous callers do not time out on burst throttling.
            pause = min(backoff, 2.0)
            logger.warning("retrying aws op=%s attempt=%d pause_s=%.2f err=%s", op, idx, pause, exc)
            time.sleep(pause)
            backoff *= 2.0


def load_runbooks(runbooks_dir: Path) -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    for file_path in sorted(runbooks_dir.rglob("*.json")):
        data = json.loads(file_path.read_text(encoding="utf-8"))
        rb_id = data["id"]
        data["_source_file"] = str(file_path.relative_to(runbooks_dir.parent))
        catalog[rb_id] = data
    return catalog


class RunbookExecutor:
    def __init__(
        self,
        runbooks_dir: Path,
        region: str = "us-east-1",
        endpoint_url: str | None = "http://localhost:4566",
        bucket: str = "day0-day2-runbook-logs",
        ssm_client: Any | None = None,
        s3_client: Any | None = None,
        client_error_cls: Any | None = None,
    ) -> None:
        self.catalog = load_runbooks(runbooks_dir)
        self.region = region
        self.bucket = bucket

        if client_error_cls is not None:
            self._client_error = client_error_cls
            boto3_mod = None
        else:
            try:
                import boto3
                from botocore.exceptions import ClientError

                boto3_mod = boto3
                self._client_error = ClientError
            except ImportError:
                boto3_mod = None

                class _FallbackError(Exception):
                    response: dict[str, Any] = {}

                self._client_error = _FallbackError

        client_kwargs: dict[str, Any] = {"region_name": region}
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url
            client_kwargs["aws_access_key_id"] = "test"
            client_kwargs["aws_secret_access_key"] = "test"

        if ssm_client is not None:
            self.ssm = ssm_client
        else:
            if boto3_mod is None:
                raise RuntimeError("boto3 is required when ssm_client is not supplied")
            self.ssm = boto3_mod.client("ssm", **client_kwargs)

        if s3_client is not None:
            self.s3 = s3_client
        else:
            if boto3_mod is None:
                raise RuntimeError("boto3 is required when s3_client is not supplied")
            self.s3 = boto3_mod.client("s3", **client_kwargs)

    def _ensure_bucket(self) -> None:
        try:
            self.s3.head_bucket(Bucket=self.bucket)
        except self._client_error:
            try:
                self.s3.create_bucket(Bucket=self.bucket)
            except self._client_error as exc:
                code = (getattr(exc, "response", {}) or {}).get("Error", {}).get("Code", "")
                if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                    raise

    def execute(
        self,
        runbook_id: str,
        incident_id: str | None = None,
        grace_extension_active: bool = False,
        now_utc: datetime | None = None,
    ) -> dict[str, Any]:
        if runbook_id not in self.catalog:
            raise RunbookError(f"unknown runbook id: {runbook_id}")

        rb = self.catalog[runbook_id]
        ts_utc = now_utc or datetime.now(timezone.utc)
        hour = ts_utc.hour

        allowed_hours = rb.get("allowed_utc_hours", [0, 23])
        start_h, end_h = int(allowed_hours[0]), int(allowed_hours[1])
        if not (start_h <= hour <= end_h) and not grace_extension_active:
            raise RunbookError(f"runbook {runbook_id} blocked outside UTC window [{start_h}..{end_h}]")

        if rb.get("requires_incident_id") and (not incident_id or len(incident_id.strip()) < 4):
            raise RunbookError(f"runbook {runbook_id} requires a valid incident_id")

        step_results: list[dict[str, Any]] = []
        for step in rb.get("steps", []):
            sname = step["name"]
            if "param_value" in step:
                _call_aws_with_retry(
                    "ssm:put_parameter",
                    lambda p=step: self.ssm.put_parameter(
                        Name=p["param_name"],
                        Value=p["param_value"],
                        Type="String",
                        Overwrite=True,
                    ),
                    self._client_error,
                )
                step_results.append({"step": sname, "status": "updated", "param": step["param_name"]})
            elif "expected_value" in step:
                resp = _call_aws_with_retry(
                    "ssm:get_parameter",
                    lambda p=step: self.ssm.get_parameter(Name=p["param_name"]),
                    self._client_error,
                )
                actual = resp.get("Parameter", {}).get("Value", "")
                matched = actual == step["expected_value"]
                step_results.append({
                    "step": sname,
                    "status": "ok" if matched else "drift_detected",
                    "param": step["param_name"],
                    "expected": step["expected_value"],
                    "actual": actual,
                })
            elif step.get("archive_to_s3"):
                self._ensure_bucket()
                key = f"runs/{rb['stage']}/{runbook_id}/{ts_utc.strftime('%Y%m%dT%H%M%SZ')}.json"
                record = {
                    "runbook_id": runbook_id,
                    "stage": rb["stage"],
                    "incident_id": incident_id,
                    "grace_extension_active": grace_extension_active,
                    "executed_at_utc": ts_utc.isoformat(),
                    "steps": step_results,
                }
                _call_aws_with_retry(
                    "s3:put_object",
                    lambda k=key, rec=record: self.s3.put_object(
                        Bucket=self.bucket,
                        Key=k,
                        Body=json.dumps(rec, indent=2).encode("utf-8"),
                        ContentType="application/json",
                    ),
                    self._client_error,
                )
                step_results.append({"step": sname, "status": "archived", "s3_key": key})

        return {
            "runbook_id": runbook_id,
            "stage": rb["stage"],
            "incident_id": incident_id,
            "grace_extension_active": grace_extension_active,
            "executed_at_utc": ts_utc.isoformat(),
            "steps": step_results,
        }
