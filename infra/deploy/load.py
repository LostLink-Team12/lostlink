"""Run real dev load with fresh tokens and preserve scale-up/drain evidence."""

import argparse
import base64
import json
import math
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from acceptance import API, login
from configure import LOCAL, Cluster, outputs, run

ROOT = Path(__file__).resolve().parents[2]


def metric(query):
    with urlopen(
        "http://127.0.0.1:9090/api/v1/query?" + urlencode({"query": query}), timeout=15
    ) as r:
        value = json.load(r)
    rows = value["data"]["result"]
    if value["status"] != "success" or len(rows) != 1:
        raise RuntimeError("Expected one actual aggregate metric series")
    result = float(rows[0]["value"][1])
    if not math.isfinite(result):
        raise RuntimeError("Metric is unavailable, not zero")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["search", "bulk"])
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--evidence-id", help="Separate evidence directory when resuming the same run"
    )
    parser.add_argument("--count", type=int, default=5000)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}", args.run_id):
        raise ValueError("Use a stable 1-40 character run ID")
    if args.evidence_id and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,59}", args.evidence_id):
        raise ValueError("Use a safe 1-60 character evidence directory name")
    os.environ.update(AWS_PROFILE="lostlink-dev", AWS_DEFAULT_REGION="ap-southeast-1")
    data = outputs()
    origin = data["c3"]["value"]["frontend_origin"]
    if args.mode == "search":
        # A t3.large can hold only one reserved 4 GiB model pod. Node count
        # alone is insufficient for eight API replicas plus the warm worker.
        slots = metric(
            'sum(floor(kube_node_status_allocatable{resource="memory",unit="byte"}/4294967296))'
        )
        cap = metric(
            'kube_horizontalpodautoscaler_spec_max_replicas{namespace="lostlink",'
            'horizontalpodautoscaler="matching-api"}'
        )
        if slots < 9 or cap < 8:
            raise RuntimeError(
                "The 200 VU profile needs an explicit eight-API HPA and at least "
                "nine reserved 4 GiB model slots, plus room for other workloads"
            )

    class C:
        kubectl = str(ROOT / "infra/terraform/.terraform/tools/kubectl.exe")
        kubeconfig = str(ROOT / "infra/terraform/.terraform/validation/kubeconfig")

    cluster = Cluster(C())
    secret = cluster.get("secret", "lostlink-acceptance")
    users = json.loads(base64.b64decode(secret["data"]["users"]))
    app, domain = data["application"]["value"], data["cognito"]["value"]["login_domain"]
    owner = login(users["owner"], app["web_client_id"], domain, origin)
    staff = login(users["staff"], app["operator_client_id"], domain, origin, venue=True)
    # Never put tokens in command arguments, files or load-generator output.
    environment = {
        **os.environ,
        "KUBECONFIG": C.kubeconfig,
        "RUN_ID": args.run_id,
        "K6_NO_USAGE_REPORT": "true",
        "MATCHING_API_URL": origin + "/api/matching",
        "ITEM_API_URL": origin + "/api/item",
    }
    api = API(origin)
    for _ in range(20):
        api.call("matching", "/v1/search", owner, {"text": "backpack", "limit": 5})
        time.sleep(1)
    metric("lostlink_embedding_runnable_jobs")
    failed_baseline = metric("lostlink_embedding_failed_jobs")
    out = ROOT / "tests/load/results" / (args.evidence_id or args.run_id)
    out.mkdir(parents=True, exist_ok=True)
    stop = out / "capture.stop"
    stop.unlink(missing_ok=True)
    summary = out / "summary.json"
    environment.update(
        LOSTLINK_ACCESS_TOKEN=owner if args.mode == "search" else staff,
        SUMMARY_PATH=str(summary),
        IMPORT_COUNT=str(args.count),
        LOAD_MANIFEST=str(ROOT / "tests/load/generated/manifest.json"),
        LOAD_PHOTOS=str(ROOT / "tests/load/generated/photos.bin"),
    )
    capture_args = [
        "node",
        "tests/load/capture-scaling.mjs",
        "--context",
        "lostlink-dev",
        "--namespace",
        "lostlink",
        "--selector",
        "app.kubernetes.io/part-of=lostlink",
        "--prometheus",
        "http://127.0.0.1:9090",
        "--backlog-query",
        "lostlink_embedding_runnable_jobs",
        "--p95-query",
        "histogram_quantile(0.95, sum by (le) "
        "(rate(lostlink_http_request_duration_seconds_bucket"
        '{namespace="lostlink",service="matching-service",route="/v1/search",method="POST"}[15m])))',
        "--seconds",
        "4200",
        "--interval",
        "15",
        "--out",
        str(out),
        "--kubectl",
        C.kubectl,
        "--stop-file",
        str(stop),
    ]
    if args.mode == "bulk":
        capture_args.append("--no-search-p95")
    started = datetime.now(UTC).isoformat()
    capture_log = (out / "capture.log").open("w", encoding="utf-8")
    collector = subprocess.Popen(
        capture_args,
        cwd=ROOT,
        env={k: v for k, v in environment.items() if k != "LOSTLINK_ACCESS_TOKEN"},
        stdout=capture_log,
        stderr=subprocess.STDOUT,
    )
    try:
        k6 = ROOT / "infra/terraform/.terraform/tools/k6/k6-v1.6.0-windows-amd64/k6.exe"
        script = "search_load.js" if args.mode == "search" else "bulk_import.js"
        with (out / "k6.log").open("w", encoding="utf-8") as log:
            load = subprocess.run(
                [str(k6), "run", "--quiet", "--no-color", "tests/load/" + script],
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        print(
            f"Real {args.mode} k6 exited {load.returncode}; waiting for cooldown/drain",
            flush=True,
        )
        deadline, stable_since = time.monotonic() + 1200, None
        replica_filter = '{namespace="lostlink",deployment=~".*-api|matching-tasks"}'
        while time.monotonic() < deadline:
            values = {
                key: metric("lostlink_embedding_" + key + "_jobs")
                for key in ["runnable", "leased", "delayed", "failed"]
            }
            desired = metric("sum(kube_deployment_spec_replicas" + replica_filter + ")")
            ready = metric("sum(kube_deployment_status_replicas_available" + replica_filter + ")")
            if values["failed"] > failed_baseline:
                raise RuntimeError(
                    "Real embedding failures remain; inspect worker/controller evidence"
                )
            if (
                all(values[key] == 0 for key in ["runnable", "leased", "delayed"])
                and desired == ready == 6
            ):
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= 180:
                    break
            else:
                stable_since = None
            if collector.poll() is not None:
                raise RuntimeError("Scaling collector exited before cooldown/drain")
            time.sleep(15)
        else:
            raise RuntimeError("Cooldown/backlog drain not observed within 20 minutes")
    finally:
        stop.write_text("stop\n", encoding="utf-8")
        collector.wait(timeout=120)
        capture_log.close()
    run(
        [
            "node",
            "tests/load/render-results.mjs",
            "--summary",
            str(summary),
            "--scaling",
            str(out / "scaling.json"),
            "--out",
            str(out / "report"),
        ]
    )
    if args.mode == "bulk" and load.returncode == 0:
        staff = login(users["staff"], app["operator_client_id"], domain, origin, venue=True)
        states, offset = {}, 0
        while True:
            page = api.call("item", f"/v1/items?limit=100&offset={offset}", staff)
            for item in page["items"]:
                states[item["state"]] = states.get(item["state"], 0) + 1
            if not page["has_more"]:
                break
            offset += 100
        (out / "inventory-states.json").write_text(json.dumps(states, indent=2), encoding="utf-8")
        if states.get("available", 0) < args.count or any(
            states.get(state, 0) for state in ["processing", "processing_failed", "awaiting_upload"]
        ):
            raise RuntimeError("Confirmed photos have not all reached searchable available state")
        owner = login(users["owner"], app["web_client_id"], domain, origin)
        result = api.call("matching", "/v1/search", owner, {"text": "backpack", "limit": 5})
        if not result["items"]:
            raise RuntimeError("No real imported item is searchable after backlog drain")
    events = json.loads(run([*cluster.command, "get", "events", "-n", "lostlink", "-o", "json"]))
    token = json.loads((LOCAL / "infrastructure.tfvars.json").read_text())["c3_origin_token"]
    evidence = [
        {
            k: event.get(k)
            for k in [
                "reason",
                "type",
                "count",
                "firstTimestamp",
                "lastTimestamp",
                "involvedObject",
                "message",
            ]
        }
        for event in events["items"]
    ]
    text = json.dumps(evidence, indent=2).replace(token, "[redacted origin header]")
    text = re.sub(r"postgresql(?:\+psycopg)?://[^\s\"']+", "[redacted connection]", text)
    (out / "controller-events.json").write_text(text, encoding="utf-8")
    (out / "run.json").write_text(
        json.dumps(
            {
                "started_at": started,
                "finished_at": datetime.now(UTC).isoformat(),
                "run_id": args.run_id,
                "mode": args.mode,
                "origin": origin,
                "k6_exit_code": load.returncode,
                "collector_exit_code": collector.returncode,
                "cooldown_and_drain_observed": True,
                "historical_failed_jobs_before": failed_baseline,
                "failed_jobs_after": values["failed"],
                "prometheus_p95_window": "15m",
                "search_latency_source": "search k6 summary",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if load.returncode or collector.returncode:
        raise RuntimeError("Load thresholds or scaling collection failed; evidence retained")
    print(
        "Real load thresholds, cooldown and backlog drain passed; evidence saved at " + str(out),
        flush=True,
    )


if __name__ == "__main__":
    main()
