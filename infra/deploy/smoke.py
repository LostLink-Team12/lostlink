"""Check the real HTTPS frontend and public/private route boundary, without tokens."""

import argparse
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from configure import outputs


def status(url):
    try:
        with urlopen(Request(url, headers={"User-Agent": "LostLink-C3-Smoke"}), timeout=30) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read(1024 * 1024)
    except HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read(65536)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--origin", help="Existing HTTPS origin; avoids Terraform state access in CD"
    )
    args = parser.parse_args()
    origin = args.origin or outputs()["c3"]["value"]["frontend_origin"]
    if not origin:
        raise RuntimeError("HTTPS edge is not deployed")
    evidence = []
    for path in ["/owner-app/", "/venue-portal/", "/matches/c3-route-test"]:
        code, content_type, body = status(origin + path)
        if code != 200 or "text/html" not in content_type or b"/assets/" not in body:
            raise RuntimeError(f"Frontend entry failed: {path} ({code})")
        evidence.append({"path": path, "status": code})
    code, _, body = status(origin + "/config.json")
    public = json.loads(body)
    if code != 200 or public["api"]["item"] != "/api/item":
        raise RuntimeError("Public runtime config is invalid")
    for service, path in [
        ("venue", "/v1/venues/current"),
        ("item", "/v1/items"),
        ("claim", "/v1/claims"),
        ("notification", "/v1/notifications"),
        ("matching", "/v1/lost-reports"),
    ]:
        url = f"/api/{service}{path}"
        code, _, body = status(origin + url)
        if code != 401 or "error" not in json.loads(body):
            raise RuntimeError(f"Public authentication boundary failed: {url} ({code})")
        evidence.append({"path": url, "status": code})
    for path in [
        "/metrics",
        "/internal/v1/members/check",
        "/api/item/metrics",
        "/api/item/internal/v1/items/example",
        "/api/item/v1/../internal/v1/items",
        "/api/item/v1/%2e%2e/internal/v1/items",
        "/api/matching/docs",
    ]:
        code, _, _ = status(origin + path)
        if code not in {400, 403, 404}:
            raise RuntimeError(f"Private route boundary failed: {path} ({code})")
        evidence.append({"path": path, "status": code})
    from configure import LOCAL

    LOCAL.mkdir(parents=True, exist_ok=True)
    (LOCAL / "public-smoke.json").write_text(
        json.dumps({"origin": origin, "checks": evidence}, indent=2), encoding="utf-8"
    )
    print(f"Passed {len(evidence)} real HTTPS entry and route-boundary checks: {origin}")


if __name__ == "__main__":
    main()
