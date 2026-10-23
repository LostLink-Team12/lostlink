"""Exercise the real k6 runner against controlled HTTP responses, never benchmark it."""

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
K6 = os.environ.get("LOSTLINK_TEST_K6")
ITEM = "00000000-0000-0000-0000-000000000001"
TENANT = "00000000-0000-0000-0000-000000000002"


@unittest.skipUnless(K6, "Set LOSTLINK_TEST_K6 to the pinned executable")
class SearchRunnerTests(unittest.TestCase):
    def run_case(self, mode):
        phases = {"warmup": 0, "measure": 0}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                phase = self.headers.get("X-LostLink-Load-Phase")
                phases[phase] += 1
                bad = phase == "measure" or mode == "warmup-empty"
                if mode == "slow" or (mode == "warmup-slow" and phase == "warmup"):
                    time.sleep(0.65)
                hit = {
                    "item_id": ITEM,
                    "tenant_id": TENANT,
                    "item_version": 1,
                    "title": "Fixture",
                    "description": "Controlled response",
                    "category": "Bags",
                    "found_at": "2026-01-01T00:00:00Z",
                    "public_location_hint": None,
                    "photo_url": None,
                    "score": 0.8,
                }
                if mode == "wrong-id" and bad:
                    hit["item_id"] = TENANT
                body = {
                    "items": [] if mode in {"empty", "warmup-empty"} and bad else [hit],
                    "limit": 1,
                    "candidate_limit": 20 if mode == "wrong-bound" and bad else 100,
                    "model_version": "fixture",
                    "potentially_truncated": False,
                }
                raw = json.dumps(body).encode()
                self.send_response(503 if mode == "http-failure" and bad else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        with tempfile.TemporaryDirectory(prefix="lostlink-k6-") as directory:
            directory = Path(directory)
            cases = directory / "cases.json"
            summary = directory / "summary.json"
            cases.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "name": "controlled",
                                "payload": {"text": "private-query-marker", "limit": 1},
                                "expected_item_ids": [ITEM],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                environment = {k: v for k, v in os.environ.items() if not k.startswith("K6_")}
                environment.update(
                    MATCHING_API_URL=f"http://127.0.0.1:{server.server_port}",
                    LOSTLINK_ACCESS_TOKEN="private-token-marker",
                    SEARCH_SCOPE="local-fixture",
                    SEARCH_PROFILE="validation",
                    SEARCH_DURATION="2s" if mode == "slow" else "1s",
                    SEARCH_MIN_SAMPLES="100000" if mode == "insufficient" else "1",
                    SEARCH_WARMUP="1",
                    SEARCH_VUS="1",
                    EXPECTED_CANDIDATE_LIMIT="100",
                    EXPECTED_MODEL_VERSION="fixture",
                    SEARCH_CASES_FILE=str(cases),
                    SEARCH_SUMMARY_FILE=str(summary),
                )
                result = subprocess.run(
                    [
                        K6,
                        "run",
                        "--quiet",
                        "--no-usage-report",
                        str(ROOT / "tests/load/search_review.js"),
                    ],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertTrue(summary.is_file(), result.stdout + result.stderr)
                report = json.loads(summary.read_text(encoding="utf-8"))
                # Summary output deliberately contains no raw response, query, ID or credential.
                for private in [ITEM, TENANT, "private-query-marker", "private-token-marker"]:
                    self.assertNotIn(private, result.stdout)
                    self.assertNotIn(private, summary.read_text(encoding="utf-8"))
                return result.returncode, report, phases
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_positive_and_warmup_excluded(self):
        code, report, phases = self.run_case("warmup-slow")
        self.assertEqual(code, 0)
        self.assertTrue(report["passed"])
        self.assertFalse(report["acceptance_eligible"])
        self.assertEqual(report["cases"][0]["samples"], phases["measure"])
        self.assertEqual(phases["warmup"], 1)
        self.assertLess(report["cases"][0]["p95_ms"], 500)

    def test_reject_invalid_results(self):
        for mode in ["empty", "wrong-id", "wrong-bound", "http-failure"]:
            with self.subTest(mode=mode):
                code, report, _ = self.run_case(mode)
                self.assertNotEqual(code, 0)
                self.assertFalse(report["passed"])
                self.assertEqual(report["cases"][0]["decision"], "invalid-responses")

    def test_reject_latency(self):
        code, report, _ = self.run_case("slow")
        self.assertNotEqual(code, 0)
        self.assertEqual(report["cases"][0]["decision"], "investigate-latency")

    def test_reject_insufficient_samples(self):
        code, report, _ = self.run_case("insufficient")
        self.assertNotEqual(code, 0)
        self.assertEqual(report["cases"][0]["decision"], "insufficient-samples")

    def test_stop_after_invalid_warmup(self):
        code, report, phases = self.run_case("warmup-empty")
        self.assertNotEqual(code, 0)
        self.assertEqual(phases["measure"], 0)
        self.assertEqual(report["cases"][0]["samples"], 0)


if __name__ == "__main__":
    unittest.main()
