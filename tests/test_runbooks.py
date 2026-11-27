from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.runbook_runner.engine import RunbookError, RunbookExecutor, sign_payload, verify_signature


class TestDay0ToDay2Runbooks(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parents[1]
        self.fake_ssm = MagicMock()
        self.fake_s3 = MagicMock()
        self.executor = RunbookExecutor(
            runbooks_dir=self.repo_root / "runbooks",
            region="us-east-1",
            endpoint_url=None,
            bucket="test-runbook-bucket",
            ssm_client=self.fake_ssm,
            s3_client=self.fake_s3,
        )

    def test_day0_day1_day2_flow(self) -> None:
        d0 = self.executor.execute("day0-ssm-bootstrap")
        self.assertEqual(d0["stage"], "day0")
        self.assertEqual(self.fake_ssm.put_parameter.call_count, 2)

        self.fake_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "https://svc.internal.local"}},
            {"Parameter": {"Value": "false"}},
        ]
        d1 = self.executor.execute("day1-config-drift")
        self.assertTrue(all(s["status"] == "ok" for s in d1["steps"]))

        d2 = self.executor.execute(
            "day2-incident-drain",
            incident_id="INC-402",
            now_utc=datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(d2["stage"], "day2")
        self.fake_s3.put_object.assert_called_once()

    def test_day2_blocks_missing_incident_and_allows_grace_extension_off_hours(self) -> None:
        with self.assertRaises(RunbookError):
            self.executor.execute(
                "day2-incident-drain",
                incident_id=None,
                now_utc=datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc),
            )

        res = self.executor.execute(
            "day2-incident-drain",
            incident_id="INC-999",
            grace_extension_active=True,
            now_utc=datetime(2026, 11, 27, 1, 0, tzinfo=timezone.utc),
        )
        self.assertTrue(res["grace_extension_active"])

    def test_hmac_signature_check(self) -> None:
        secret = "runbook-secret"
        body = b'{"runbook_id":"day1-config-drift"}'
        now = 1764267000
        sig = sign_payload(secret, now, body)
        ok, _ = verify_signature(secret, sig, str(now), body, now_unix=now)
        self.assertTrue(ok)

    def test_bench_signature_verification(self) -> None:
        secret = "bench-secret"
        body = b'{"runbook_id":"day1-config-drift"}'
        now = int(time.time())
        sig = sign_payload(secret, now, body)
        iters = 5000
        t0 = time.perf_counter()
        for _ in range(iters):
            ok, _ = verify_signature(secret, sig, str(now), body, now_unix=now)
            self.assertTrue(ok)
        ms = (time.perf_counter() - t0) * 1000.0
        print(f"BENCH verify_signature: {iters} calls in {ms:.2f} ms ({(ms / iters) * 1000:.2f} us/call)")


if __name__ == "__main__":
    unittest.main()
