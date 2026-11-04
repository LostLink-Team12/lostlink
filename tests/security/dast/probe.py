"""Send bounded candidate traffic through ZAP; publish no HTTP bodies or secrets."""

import json
import re
import ssl
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen

ORIGIN = "https://frontend:8443"
API = "http://zap:8080/JSON/"
SERVICES = ("venue", "item", "matching", "claim", "notification")
PUBLIC = ("/owner-app/", "/venue-portal/", "/matches/dast-candidate", "/config.json")
ENDPOINTS = {
    "venue": "/v1/venues/current",
    "item": "/v1/items",
    "matching": "/v1/lost-reports",
    "claim": "/v1/claims",
    "notification": "/v1/notifications",
}
PRIVATE = ("/metrics", "/api/item/internal/v1/items/example", "/api/matching/docs")


class CheckFailure(RuntimeError):
    """Safe, predefined test-case name for diagnosing a failed boundary assertion."""


def zap(path, **params):
    with urlopen(API + path + "/?" + urlencode(params), timeout=30) as response:
        return json.loads(response.read(8 * 1024 * 1024))


def wait(check, seconds=180):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError("Candidate component did not become available")


def check_path(path):
    url = urlsplit(ORIGIN + path)
    if not path.startswith("/") or url.scheme != "https" or url.netloc != "frontend:8443":
        raise ValueError("Only the isolated candidate origin is in scope")
    if url.fragment or url.username or path.startswith("//") or any(c in path for c in "\r\n"):
        raise ValueError("Invalid candidate request")


def request(path, *, token=None, method="GET", body=None):
    check_path(path)
    if method != "GET" or body is not None or token and any(c in token for c in "\r\n"):
        raise ValueError("DAST traffic is restricted to approved read-only requests")
    headers = f"{method} {ORIGIN}{path} HTTP/1.1\r\nHost: frontend:8443\r\n"
    if token:
        headers += f"Authorization: Bearer {token}\r\n"
    payload = json.dumps(body) if body is not None else ""
    if body is not None:
        headers += f"Content-Type: application/json\r\nContent-Length: {len(payload.encode())}\r\n"
    result = zap(
        "core/action/sendRequest", request=headers + "\r\n" + payload, followRedirects="false"
    )
    messages = result.get("sendRequest")
    if not isinstance(messages, list) or len(messages) != 1:
        raise RuntimeError("ZAP did not return one HTTP exchange")
    message = messages[0]
    status = int(message["responseHeader"].split(" ", 2)[1])
    return status, message.get("responseBody", ""), message["responseHeader"]


def alerts_verdict(data):
    if not isinstance(data, list):
        raise ValueError("Missing ZAP alerts")
    safe = []
    for alert in data:
        risk = {"Informational": 0, "Low": 1, "Medium": 2, "High": 3}.get(alert.get("risk"))
        if risk is None:
            risk = int(alert["riskCode"])
        url = urlsplit(alert["url"])
        if risk not in range(4) or url.scheme != "https" or url.netloc != "frontend:8443":
            raise ValueError("Invalid or out-of-scope ZAP evidence")
        safe.append(
            {
                "rule": alert["pluginId"],
                "risk": risk,
                "path": url.path,
                "blocking": risk >= 2,
            }
        )
    return safe


def contract_get(path):
    parts = path.split("/", 3)
    if len(parts) != 4 or parts[1] != "api" or parts[2] not in SERVICES:
        raise ValueError("Not a public service contract path")
    contract = json.loads(Path(f"/contracts/{parts[2]}-service.openapi.json").read_text())
    for route, methods in contract["paths"].items():
        if not route.startswith("/v1/") or "get" not in methods:
            continue
        pattern = re.sub(r"\{[^}]+\}", "[^/]+", route)
        if re.fullmatch(pattern, "/" + parts[3].split("?", 1)[0]):
            return
    raise ValueError("Request is not an approved public GET operation")


def authenticated():
    from cryptography.hazmat.primitives.asymmetric import rsa
    from identity import mint
    from seed import ITEMS, REPORTS, TENANTS

    owner = mint("dast-owner-a")
    other_owner = mint("dast-owner-b")
    staff = mint("dast-staff-a", "dast-staff", ("venue_admin",), TENANTS[0])
    other_staff = mint("dast-staff-b", "dast-staff", ("venue_admin",), TENANTS[1])
    checks = []

    def expect(name, path, token, expected):
        contract_get(path)
        status, body, _ = request(path, token=token)
        checks.append({"case": name, "path": path, "status": status, "expected": expected})
        if status != expected:
            raise CheckFailure(name)
        return json.loads(body)

    for service in ("matching", "claim", "notification"):
        expect(f"owner-{service}", f"/api/{service}{ENDPOINTS[service]}", owner, 200)
    expect("staff-current-venue", "/api/venue/v1/venues/current", staff, 200)
    for token, index in ((staff, 0), (other_staff, 1)):
        data = expect(f"staff-items-{index}", "/api/item/v1/items", token, 200)
        if {item["id"] for item in data["items"]} != {ITEMS[index]}:
            raise RuntimeError("Tenant list isolation failed")
        expect(f"staff-own-item-{index}", f"/api/item/v1/items/{ITEMS[index]}", token, 200)
        expect(f"staff-other-item-{index}", f"/api/item/v1/items/{ITEMS[1 - index]}", token, 404)
    for token, index in ((owner, 0), (other_owner, 1)):
        data = expect(f"owner-reports-{index}", "/api/matching/v1/lost-reports", token, 200)
        if {item["id"] for item in data["items"]} != {REPORTS[index]}:
            raise RuntimeError("Owner list isolation failed")
        expect(
            f"owner-other-report-{index}",
            f"/api/matching/v1/lost-reports/{REPORTS[1 - index]}",
            token,
            404,
        )
    expect("owner-denied-staff-api", "/api/item/v1/items", owner, 403)
    expect("staff-denied-owner-api", "/api/matching/v1/lost-reports", staff, 403)
    wrong_tenant = mint("dast-staff-a", "dast-staff", ("venue_admin",), TENANTS[1])
    expect("current-membership-mismatch", "/api/item/v1/items", wrong_tenant, 403)
    for name, changes in (
        ("expired", {"exp": int(time.time()) - 60}),
        ("wrong-issuer", {"iss": "https://other.invalid"}),
        ("wrong-audience", {"aud": "other"}),
        ("wrong-client", {"client_id": "other"}),
        ("id-token", {"token_use": "id"}),
    ):
        expect(name, "/api/matching/v1/lost-reports", mint("dast-owner-a", overrides=changes), 401)
    forged = mint(
        "dast-owner-a", key=rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    expect("wrong-signature", "/api/matching/v1/lost-reports", forged, 401)
    return checks


def run():
    context = ssl.create_default_context(cafile="/private/ca.pem")
    for service in SERVICES:

        def health(name=service):
            with urlopen(
                f"https://{name}-api.lostlink.svc.cluster.local:8443/health",
                context=context,
                timeout=5,
            ) as r:
                return r.status == 200

        wait(health)
    wait(lambda: bool(zap("core/view/version").get("version")))
    checks = []
    assets = set()
    for path in PUBLIC:
        status, body, _ = request(path)
        if status != 200:
            raise RuntimeError("Candidate frontend entry failed")
        checks.append({"path": path, "status": status})
        assets.update(
            p for p in re.findall(r'(?:src|href)="([^"]+)"', body) if p.startswith("/assets/")
        )
    if not assets:
        raise RuntimeError("No candidate JavaScript/CSS assets were exercised")
    for path in sorted(assets):
        status, _, _ = request(path)
        if status != 200:
            raise RuntimeError("Candidate asset failed")
        checks.append({"path": path, "status": status})
    for service, endpoint in ENDPOINTS.items():
        path = f"/api/{service}{endpoint}"
        status, _, _ = request(path)
        if status != 401:
            raise RuntimeError("Anonymous API authentication boundary failed")
        checks.append({"path": path, "status": status})
    for path in PRIVATE:
        status, _, _ = request(path)
        if status not in {400, 403, 404}:
            raise RuntimeError("Private route became public")
        checks.append({"path": path, "status": status})
    checks.extend(authenticated())
    wait(lambda: zap("pscan/view/recordsToScan")["recordsToScan"] == "0")
    raw = zap("core/view/alerts", baseurl=ORIGIN, start=0, count=10000)
    safe = alerts_verdict(raw.get("alerts"))
    return {
        "gate": "failed" if any(a["blocking"] for a in safe) else "passed",
        "checks": checks,
        "alerts": safe,
        "mode": "passive-explicit-traffic",
    }


if __name__ == "__main__":
    output = {"gate": "error"}
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "bootstrap":
            import boto3

            client = boto3.client(
                "s3",
                endpoint_url="http://localstack:4566",
                region_name="ap-southeast-1",
                aws_access_key_id="test",
                aws_secret_access_key="test",
            )
            client.create_bucket(
                Bucket="dast-media",
                CreateBucketConfiguration={"LocationConstraint": "ap-southeast-1"},
            )
            raise SystemExit(0)
        output = run()
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        output["error"] = type(exc).__name__
        if isinstance(exc, CheckFailure):
            output["failed_case"] = str(exc)
    Path("/evidence/result.json").write_text(json.dumps(output, indent=2), encoding="utf-8")
    raise SystemExit(int(output["gate"] != "passed"))
