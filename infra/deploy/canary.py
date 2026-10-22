"""Low-rate real owner searches during import; stores only timing/status counts."""

import argparse
import base64
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from acceptance import API, login
from configure import Cluster, outputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    out = Path(args.run_dir).resolve()
    if not out.is_relative_to(root / "tests/load/results"):
        raise ValueError("Use a load results directory inside this checkout")
    os.environ.update(AWS_PROFILE="lostlink-dev", AWS_DEFAULT_REGION="ap-southeast-1")
    data = outputs()

    class C:
        kubectl = str(root / "infra/terraform/.terraform/tools/kubectl.exe")
        kubeconfig = str(root / "infra/terraform/.terraform/validation/kubeconfig")

    users = json.loads(
        base64.b64decode(Cluster(C()).get("secret", "lostlink-acceptance")["data"]["users"])
    )
    origin = data["c3"]["value"]["frontend_origin"]
    token = login(
        users["owner"],
        data["application"]["value"]["web_client_id"],
        data["cognito"]["value"]["login_domain"],
        origin,
    )
    issued = time.monotonic()
    samples = []
    api = API(origin)
    while not (out / "capture.stop").exists():
        if time.monotonic() - issued > 2700:
            token = login(
                users["owner"],
                data["application"]["value"]["web_client_id"],
                data["cognito"]["value"]["login_domain"],
                origin,
            )
            issued = time.monotonic()
        started = time.perf_counter()
        result = api.call("matching", "/v1/search", token, {"text": "backpack", "limit": 5})
        samples.append(
            {
                "time": datetime.now(UTC).isoformat(),
                "latency_ms": (time.perf_counter() - started) * 1000,
                "items_count": len(result["items"]),
                "status": 200,
            }
        )
        (out / "search-canary.json").write_text(
            json.dumps(
                {
                    "interval_seconds": 30,
                    "scenario": "one low-rate owner search during bulk import",
                    "samples": samples,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        time.sleep(30)
    print(f"Completed {len(samples)} real low-rate owner search canaries")


if __name__ == "__main__":
    main()
