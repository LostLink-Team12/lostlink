"""Real dev business acceptance using isolated Cognito fixtures and code/PKCE.

No invitations are sent. Trusted synthetic fixture identities are seeded by the
deployment administrator; this does not test mailbox ownership verification.
Notification delivery is paused during synthetic tests and reports are closed
before the sender is restored. Passwords/tokens stay in memory or Kubernetes.
"""

import argparse
import base64
import hashlib
import http.cookiejar
import json
import os
import secrets
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen
from uuid import uuid4

import boto3
from botocore.config import Config
from configure import LOCAL, Cluster, outputs


class Form(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = {}
        self.action = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and attrs.get("method", "").lower() == "post":
            self.action = attrs.get("action")
        if tag == "input" and attrs.get("type") == "hidden" and attrs.get("name"):
            self.fields[attrs["name"]] = attrs.get("value", "")


def login(user, client_id, domain, origin, venue=False):
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    redirect = origin + ("/venue-portal/" if venue else "/owner-app/")
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": redirect,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
    }
    opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
    headers = {"User-Agent": "Mozilla/5.0 LostLink-C3-Acceptance"}
    with opener.open(
        Request(domain + "/oauth2/authorize?" + urlencode(params), headers=headers), timeout=30
    ) as response:
        form = Form()
        form.feed(response.read().decode())
        page = response.geturl()
    if not form.action:
        raise RuntimeError("Cognito login did not provide a password form")
    if urlsplit(urljoin(page, form.action)).netloc != urlsplit(domain).netloc:
        raise RuntimeError("Cognito password form changed origin")
    form.fields.update(
        username=user["username"], password=user["password"], signInSubmitButton="Sign in"
    )
    request = Request(
        urljoin(page, form.action),
        data=urlencode(form.fields).encode(),
        headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
    )
    with opener.open(request, timeout=30) as response:
        callback = response.geturl()
        response.read()
    query = parse_qs(urlsplit(callback).query)
    if query.get("state") != [state] or "code" not in query:
        raise RuntimeError("Cognito code/PKCE login failed")
    with urlopen(
        Request(
            domain + "/oauth2/token",
            data=urlencode(
                {
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "redirect_uri": redirect,
                    "code": query["code"][0],
                    "code_verifier": verifier,
                }
            ).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ),
        timeout=30,
    ) as response:
        token = json.load(response)
    if not token.get("access_token"):
        raise RuntimeError("Cognito did not return an access token")
    return token["access_token"]


class API:
    def __init__(self, origin):
        self.origin = origin
        self.checks = []

    def call(self, service, path, token, body=None, *, method=None, version=None, expected=None):
        headers = {"Authorization": "Bearer " + token}
        method = method or ("POST" if body is not None else "GET")
        if method != "GET":
            headers.update({"Content-Type": "application/json", "Idempotency-Key": str(uuid4())})
        if version is not None:
            headers["If-Match"] = f'"{version}"'
        request = Request(
            self.origin + "/api/" + service + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=60) as response:
                code, value = response.status, json.load(response)
        except HTTPError as exc:
            code, value = exc.code, json.loads(exc.read())
        accepted_codes = {expected} if isinstance(expected, int) else set(expected or [])
        if expected is not None and code not in accepted_codes:
            raise RuntimeError(
                f"{service} {method} failed: HTTP {code}; "
                f"code={value.get('error', {}).get('code', 'unknown')}"
            )
        if expected is None and not 200 <= code < 300:
            raise RuntimeError(
                f"{service} {method} failed: HTTP {code}; "
                f"code={value.get('error', {}).get('code', 'unknown')}"
            )
        self.checks.append(
            {"service": service, "method": method, "path": path.split("?")[0], "status": code}
        )
        return value

    def wait(self, service, path, token, predicate, timeout=300):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.call(service, path, token)
            if predicate(value):
                return value
            time.sleep(2)
        raise RuntimeError(f"Timed out waiting for {service} business operation")


def fixtures(cluster, client, pool):
    existing = cluster.get("secret", "lostlink-acceptance")
    users = json.loads(base64.b64decode(existing["data"]["users"])) if existing else {}
    suffix = secrets.token_hex(4)
    for name in ["platform", "staff", "other_staff", "employee", "owner", "unverified"]:
        if name in users:
            continue
        email = f"c3-{name.replace('_', '-')}-{suffix}@example.com"
        created = client.admin_create_user(
            UserPoolId=pool,
            Username=email,
            MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "false" if name == "unverified" else "true"},
            ],
        )
        password = secrets.token_urlsafe(36) + "aA1!"
        client.admin_set_user_password(
            UserPoolId=pool, Username=email, Password=password, Permanent=True
        )
        users[name] = {
            "username": email,
            "password": password,
            "sub": next(x["Value"] for x in created["User"]["Attributes"] if x["Name"] == "sub"),
        }
    cluster.secret("lostlink-acceptance", {"users": json.dumps(users)})
    return users


def staff_flow(cluster, data, users):
    app, origin = data["application"]["value"], data["c3"]["value"]["frontend_origin"]
    domain = data["cognito"]["value"]["login_domain"]
    staff = login(users["staff"], app["operator_client_id"], domain, origin, venue=True)
    api = API(origin)
    path = "/v1/venues/current/members"
    members = api.call("venue", path, staff)["items"]
    member = next((m for m in members if m["user_sub"] == users["employee"]["sub"]), None)
    if member is None:
        member = api.call(
            "venue", path, staff, {"user_sub": users["employee"]["sub"], "role": "venue_staff"}
        )
    member_path = path + "/" + member["id"]
    member = api.call(
        "venue",
        member_path,
        staff,
        {"role": "venue_admin", "status": "active"},
        method="PATCH",
        version=member["version"],
    )
    employee = login(users["employee"], app["operator_client_id"], domain, origin, venue=True)
    api.call("venue", path, employee)
    member = api.call(
        "venue",
        member_path,
        staff,
        {"status": "suspended"},
        method="PATCH",
        version=member["version"],
    )
    api.call("venue", "/v1/venues/current", employee, expected=403)
    member = api.call(
        "venue",
        member_path,
        staff,
        {"role": "venue_staff", "status": "active"},
        method="PATCH",
        version=member["version"],
    )
    employee = login(users["employee"], app["operator_client_id"], domain, origin, venue=True)
    api.call("venue", "/v1/venues/current", employee)
    (LOCAL / "staff-acceptance.json").write_text(
        json.dumps(
            {
                "completed_at": datetime.now(UTC).isoformat(),
                "origin": origin,
                "checks": api.checks,
                "final_role": member["role"],
                "final_status": member["status"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        "Real staff add, role change, immediate suspension enforcement and reactivation passed",
        flush=True,
    )


def run_flow(cluster, data, client, users):
    app, origin = data["application"]["value"], data["c3"]["value"]["frontend_origin"]
    domain = data["cognito"]["value"]["login_domain"]
    pool, operator, owner_client = (
        app["user_pool_id"],
        app["operator_client_id"],
        app["web_client_id"],
    )
    api = API(origin)
    sender = cluster.get("deployment", "notification-tasks")
    if sender and sender["spec"].get("replicas", 1) != 0:
        raise RuntimeError("Pause notification-tasks before testing synthetic recipients")

    def auth(name, venue=False):
        return login(users[name], operator if venue else owner_client, domain, origin, venue)

    venues = []
    missing = []
    for name in ["staff", "other_staff"]:
        current = api.call(
            "venue", "/v1/venues/current", auth(name, True), expected=(200, 403, 404)
        )
        venues.append(current.get("id"))
        if "id" not in current:
            missing.append(name)
    # Grant privilege only to our new controlled bootstrap fixture; always remove it.
    if missing:
        client.admin_add_user_to_group(
            UserPoolId=pool, Username=users["platform"]["username"], GroupName="platform_admin"
        )
        try:
            platform = auth("platform", True)
            for name in missing:
                slug = "c3-" + name.replace("_", "-") + "-" + secrets.token_hex(4)
                value = api.call(
                    "venue",
                    "/v1/admin/venues",
                    platform,
                    {
                        "name": "C3 acceptance " + name,
                        "slug": slug,
                        "initial_admin_sub": users[name]["sub"],
                    },
                )
                venues[["staff", "other_staff"].index(name)] = value["venue"]["id"]
        finally:
            client.admin_remove_user_from_group(
                UserPoolId=pool, Username=users["platform"]["username"], GroupName="platform_admin"
            )
            client.admin_user_global_sign_out(
                UserPoolId=pool, Username=users["platform"]["username"]
            )
    time.sleep(5)
    staff, other, owner = auth("staff", True), auth("other_staff", True), auth("owner")
    api.call("venue", "/v1/venues/current", staff)
    api.call("venue", "/v1/venues/current", other)
    api.call("venue", "/v1/venues/current/members", staff)
    cluster.secret(
        "lostlink-load-tokens",
        {"owner": owner, "staff": staff, "issued_at": datetime.now(UTC).isoformat()},
    )
    print("Real Cognito code/PKCE login and two-venue onboarding passed", flush=True)
    photo = next(
        (Path(__file__).resolve().parents[2] / "tests/data/match_photos").glob("*.jpg")
    ).read_bytes()
    item = api.call(
        "item",
        "/v1/items",
        staff,
        {
            "title": "C3 red backpack",
            "description": "A red backpack with a black zipper",
            "category": "Backpack",
            "found_at": datetime.now(UTC).isoformat(),
            "verification_questions": [
                {"prompt": "What is inside the pocket?", "reference_answer": "C3-blue-key"}
            ],
        },
    )
    item_id = item["id"]
    api.call("item", f"/v1/items/{item_id}", other, expected=404)
    ticket = api.call(
        "item",
        f"/v1/items/{item_id}/uploads",
        staff,
        {"content_type": "image/jpeg", "size": len(photo)},
        version=item["version"],
    )
    boundary = "lostlink-" + uuid4().hex
    chunks = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, v in ticket["upload"]["fields"].items()
    ]
    chunks += [
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            'filename="photo.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'
        ).encode(),
        photo,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    with urlopen(
        Request(
            ticket["upload"]["url"],
            data=b"".join(chunks),
            headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
        ),
        timeout=60,
    ) as r:
        if r.status not in {200, 201, 204}:
            raise RuntimeError("Real presigned S3 upload failed")
    item = api.call("item", f"/v1/items/{item_id}", staff)
    accepted = api.call(
        "item",
        f"/v1/items/{item_id}/uploads/{ticket['upload_id']}/confirm",
        staff,
        {},
        version=item["version"],
    )
    api.wait(
        "item",
        f"/v1/item-operations/{accepted['operation_id']}",
        staff,
        lambda v: v["state"] == "completed",
    )
    item = api.wait(
        "item", f"/v1/items/{item_id}", staff, lambda v: v["state"] == "available", timeout=600
    )
    print("Real S3 seal, event delivery, CLIP embedding and available state passed", flush=True)
    search = api.call(
        "matching", "/v1/search", owner, {"text": "red backpack", "venue_id": venues[0]}
    )
    if item_id not in [v["item_id"] for v in search["items"]]:
        raise RuntimeError("Embedded item is absent from real search")
    report = api.call(
        "matching",
        "/v1/lost-reports",
        owner,
        {
            "description": "A red backpack with a black zipper",
            "venue_id": venues[0],
            "photo_base64": base64.b64encode(photo).decode(),
        },
    )
    matches = api.wait(
        "matching",
        f"/v1/lost-reports/{report['id']}/matches",
        owner,
        lambda v: any(m["item"]["item_id"] == item_id for m in v["items"]),
    )
    match_id = next(m["id"] for m in matches["items"] if m["item"]["item_id"] == item_id)
    notifications = api.wait(
        "notification",
        "/v1/notifications",
        owner,
        lambda v: any(n["match_id"] == match_id for n in v["items"]),
    )
    report = api.call("matching", f"/v1/lost-reports/{report['id']}", owner)
    api.call(
        "matching",
        f"/v1/lost-reports/{report['id']}/close",
        owner,
        {"reason": "found"},
        version=report["version"],
    )
    print(
        "Real report matching and Notification inbox passed; report closed before sender restore",
        flush=True,
    )
    questions = api.call("claim", "/v1/claim-questions?item_id=" + item_id, owner)
    answers = [
        {"question_id": q["id"], "version": q["version"], "answer": "C3-blue-key"}
        for q in questions["questions"]
    ]
    claim = api.call("claim", "/v1/claims", owner, {"item_id": item_id, "answers": answers})
    api.call(
        "claim",
        "/v1/claims",
        auth("unverified"),
        {"item_id": item_id, "answers": answers},
        expected=409,
    )
    review = api.call("claim", f"/v1/venue/claims/{claim['id']}", staff)
    api.call(
        "claim",
        f"/v1/venue/claims/{claim['id']}/approve",
        staff,
        {},
        version=review["version"] + 1,
        expected=412,
    )
    approved = api.call(
        "claim", f"/v1/venue/claims/{claim['id']}/approve", staff, {}, version=review["version"]
    )
    api.wait(
        "claim",
        f"/v1/claim-operations/{approved['operation_id']}",
        staff,
        lambda v: v["state"] == "completed" and v["claim_state"] == "approved",
    )
    token = api.call("claim", f"/v1/claims/{claim['id']}/handover-token", owner, {})
    review = api.call("claim", f"/v1/venue/claims/{claim['id']}", staff)
    handed = api.call(
        "claim",
        f"/v1/venue/claims/{claim['id']}/handover",
        staff,
        {"token": token["qr_payload"]},
        version=review["version"],
    )
    api.wait(
        "claim",
        f"/v1/claim-operations/{handed['operation_id']}",
        staff,
        lambda v: v["state"] == "completed" and v["claim_state"] == "handed_over",
    )
    final_item = api.wait(
        "item", f"/v1/items/{item_id}", staff, lambda v: v["state"] == "handed_over"
    )
    (LOCAL / "business-acceptance.json").write_text(
        json.dumps(
            {
                "origin": origin,
                "completed_at": datetime.now(UTC).isoformat(),
                "venues": venues,
                "item_id": item_id,
                "claim_id": claim["id"],
                "final_item_state": final_item["state"],
                "report_id": report["id"],
                "match_id": match_id,
                "notification_inbox_observed": bool(notifications["items"]),
                "checks": api.checks,
                "email_delivery_tested": False,
                "camera_tested": False,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        "Real search, claim, stale-version rejection, approval and QR-payload handover passed",
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["seed", "flow", "staff-flow"])
    args = parser.parse_args()
    os.environ.update(AWS_PROFILE="lostlink-dev", AWS_DEFAULT_REGION="ap-southeast-1")
    data = outputs()
    root = Path(__file__).resolve().parents[2]

    class ClusterArgs:
        kubectl = str(root / "infra/terraform/.terraform/tools/kubectl.exe")
        kubeconfig = str(root / "infra/terraform/.terraform/validation/kubeconfig")

    cluster = Cluster(ClusterArgs())
    session = boto3.Session(profile_name="lostlink-dev", region_name="ap-southeast-1")
    cluster.validate(session, data)
    client = session.client(
        "cognito-idp",
        config=Config(
            connect_timeout=5,
            read_timeout=20,
            retries={"mode": "standard", "total_max_attempts": 3},
        ),
    )
    try:
        users = fixtures(cluster, client, data["application"]["value"]["user_pool_id"])
        if args.action == "flow":
            run_flow(cluster, data, client, users)
        elif args.action == "staff-flow":
            staff_flow(cluster, data, users)
        else:
            print(f"Prepared {len(users)} isolated Cognito fixtures without sending invitations")
    finally:
        client.close()


if __name__ == "__main__":
    main()
