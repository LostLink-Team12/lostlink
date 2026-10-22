"""Issue #7 diagnostic: real CLIP + pgvector + two loopback HTTP services.

Creates/drops ONLY a random disposable database and its six runtime roles. Identity
verification and HTTPS transport are injected test fixtures, never production settings.
"""

import argparse
import base64
import contextlib
import contextvars
import io
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import backend_admin as admin  # noqa: E402
import httpx  # noqa: E402
import uvicorn  # noqa: E402
from item_app.config import ItemSettings  # noqa: E402
from item_app.database import ItemDatabase  # noqa: E402
from item_app.main import create_app as item_app  # noqa: E402
from item_app.models import Item, ItemLocator  # noqa: E402
from lostlink_common.auth import InvalidToken, Principal  # noqa: E402
from lostlink_common.config import Settings  # noqa: E402
from matching_app.clients import ItemClient  # noqa: E402
from matching_app.config import MatchingSettings  # noqa: E402
from matching_app.database import MatchingDatabase  # noqa: E402
from matching_app.embedding import ClipEncoder  # noqa: E402
from matching_app.main import create_app as matching_app  # noqa: E402
from matching_app.models import Catalog, ItemVector  # noqa: E402
from PIL import Image  # noqa: E402
from psycopg import sql  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

CURRENT = contextvars.ContextVar("search_review_timing", default=None)


@contextlib.contextmanager
def timing(component):
    started = time.perf_counter()
    try:
        yield
    finally:
        row = CURRENT.get()
        if row is not None:
            row[component + "_ms"] += (time.perf_counter() - started) * 1000
            if component == "item":
                row["item_calls"] += 1


class TimedEncoder(ClipEncoder):
    def encode(self, text=None, photo=None):
        with timing("clip"):
            return super().encode(text, photo)


class TimedDatabase(MatchingDatabase):
    @contextlib.contextmanager
    def context(self, **kwargs):
        with timing("database"), super().context(**kwargs) as session:
            yield session


class TimedItem(ItemClient):
    def context(self, item_id):
        with timing("item"):
            return super().context(item_id)


@contextlib.contextmanager
def serve(app):
    """Prebind an ephemeral loopback port; never expose fixture identities on the LAN."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("Local service did not start")
            time.sleep(0.05)
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=35)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("Local service did not stop")


@contextlib.contextmanager
def disposable_database():
    """Reuse step 2 role/bootstrap/migration tooling in a newly created test database."""
    raw = os.environ.get("LOSTLINK_TEST_ADMIN_URL", "")
    base = make_url(raw)
    if base.drivername != "postgresql+psycopg" or base.host not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("LOSTLINK_TEST_ADMIN_URL must be a local PostgreSQL SQLAlchemy URL")
    suffix = uuid4().hex
    name = "ll_search_review_" + suffix
    target = base.set(database=name)
    config = admin.load(ROOT / "infra/database/backend.example.json")
    config.database = name
    environment = {config.bootstrap_url_env: target.render_as_string(hide_password=False)}
    roles = []
    urls = {}
    for service, settings in config.services.items():
        environment[settings.migration_url_env] = target.render_as_string(hide_password=False)
        for mode, role in settings.roles.items():
            role.name = f"ll_sr_{service}_{mode}_{suffix}"
            password = secrets.token_urlsafe(32)
            environment[role.password_env] = password
            if service in {"item", "matching"}:
                roles.append(role.name)
                urls[f"{service}_{mode}"] = target.set(username=role.name, password=password)
    # Validate the generated names before any create or cleanup operation.
    config = admin.Deployment.model_validate(config.model_dump())
    previous = {key: os.environ.get(key) for key in environment}
    databases = []
    with admin.connect(base) as connection:
        connection.autocommit = True
        created = False
        try:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            created = True
            os.environ.update(environment)
            admin.bootstrap(config, ["item", "matching"], "local")
            admin.migrate(config, ["item", "matching"], "local")
            dbs = {}
            for key, url in urls.items():
                cls = TimedDatabase if key.startswith("matching") else ItemDatabase
                dbs[key] = cls(
                    Settings(
                        _env_file=None,
                        environment="test",
                        database_url=url.render_as_string(hide_password=False),
                    )
                )
                databases.append(dbs[key])
            yield target, dbs
        finally:
            for db in databases:
                db.close()
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            if created:
                # name and roles are generated above, never copied from the supplied URL.
                connection.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
                )
                for role in roles:
                    connection.execute(
                        sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role))
                    )


def seed(target, encoder, count):
    """Equal genuine vectors give deterministic UUID tie order; only the last hit is live."""
    tenant, correlation = uuid4(), uuid4()
    vector = encoder.encode("a solid red square")
    engine = create_engine(target)
    try:
        with Session(engine) as session, session.begin():
            for number in range(1, count + 1):
                item_id = UUID(int=number)
                shared = {
                    "tenant_id": tenant,
                    "item_version": 1,
                    "title": "Synthetic red square",
                    "description": "Issue 7 disposable diagnostic fixture",
                    "category": "Fixture",
                    "found_at": datetime(2026, 1, 1, tzinfo=UTC),
                }
                session.add(
                    Item(
                        id=item_id,
                        **shared,
                        version=1,
                        state="available" if number == count else "withdrawn",
                        processing_correlation_id=correlation,
                        created_by="search-fixture",
                    )
                )
                session.add(ItemLocator(item_id=item_id, tenant_id=tenant))
                session.add(Catalog(item_id=item_id, **shared, revision=1, state="available"))
                session.add(
                    ItemVector(
                        item_id=item_id,
                        tenant_id=tenant,
                        item_version=1,
                        source_revision=1,
                        correlation_id=correlation,
                        model_version=encoder.model_version,
                        embedding=vector,
                    )
                )
    finally:
        engine.dispose()
    return tenant


def percentile(values, fraction):
    values = sorted(values)
    position = (len(values) - 1) * fraction
    low = int(position)
    return values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (position - low)


def diagnostics(rows):
    result = {}
    for phase in ["warmup", "measure"]:
        selected = [row for row in rows if row["phase"] == phase]
        result[phase] = {"requests": len(selected)}
        for key in ["clip_ms", "database_ms", "item_ms", "item_calls"]:
            values = [row[key] for row in selected]
            result[phase][key] = (
                {
                    "mean": sum(values) / len(values),
                    "p95": percentile(values, 0.95),
                    "min": min(values),
                    "max": max(values),
                }
                if values
                else None
            )
    return result


def run(args):
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    os.environ.setdefault("MKL_NUM_THREADS", "2")
    encoder = TimedEncoder(str(args.checkpoint.resolve()), args.sha256)
    encoder.ready()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    stream = io.BytesIO()
    Image.new("RGB", (224, 224), "red").save(stream, format="PNG")
    photo = base64.b64encode(stream.getvalue()).decode()
    tokens = {
        secrets.token_urlsafe(32): Principal(
            subject="search-fixture-owner", client_id="fixture-web", roles={"owner"}
        ),
        secrets.token_urlsafe(32): Principal(
            subject="search-fixture-index",
            client_id="fixture-index",
            scopes={"lostlink/items.index"},
        ),
    }
    owner_token, index_token = tokens

    class Verifier:
        def verify(self, token):
            if token not in tokens:
                raise InvalidToken()
            return tokens[token]

    with disposable_database() as (target, dbs), contextlib.ExitStack() as stack:
        tenant = seed(target, encoder, args.candidates)
        item = item_app(
            ItemSettings(
                _env_file=None,
                environment="test",
                log_level="ERROR",
                index_client_ids=["fixture-index"],
            ),
            token_verifier=Verifier(),
            database=dbs["item_api"],
            internal_database=dbs["item_internal"],
        )
        item_url = stack.enter_context(serve(item))
        client = stack.enter_context(
            httpx.Client(timeout=10, follow_redirects=False, trust_env=False)
        )

        def transport(url, headers):
            if args.item_delay_ms:
                time.sleep(args.item_delay_ms / 1000)
            response = client.get(item_url + urlsplit(url).path, headers=headers)
            response.raise_for_status()
            return response.json()

        matching = matching_app(
            MatchingSettings(
                _env_file=None,
                environment="test",
                log_level="ERROR",
                user_client_ids=["fixture-web"],
                candidate_limit=max(20, args.candidates),
            ),
            token_verifier=Verifier(),
            database=dbs["matching_api"],
            internal_database=dbs["matching_internal"],
            encoder=encoder,
            item_client=TimedItem(
                "https://fixture.invalid", lambda: index_token, transport=transport
            ),
        )
        rows = []

        @matching.middleware("http")
        async def capture(request, call_next):
            row = {
                "phase": request.headers.get("X-LostLink-Load-Phase"),
                "clip_ms": 0,
                "database_ms": 0,
                "item_ms": 0,
                "item_calls": 0,
            }
            token = CURRENT.set(row)
            try:
                return await call_next(request)
            finally:
                CURRENT.reset(token)
                if row["phase"] in {"warmup", "measure"}:
                    rows.append(row)

        matching_url = stack.enter_context(serve(matching))
        cases = []
        for mode in args.modes.split(","):
            payload = {"limit": 1, "min_score": -1, "category": "Fixture", "venue_id": str(tenant)}
            if mode in {"text", "fused"}:
                payload["text"] = "a solid red square"
            if mode in {"photo", "fused"}:
                payload["photo_base64"] = photo
            cases.append(
                {
                    "name": mode,
                    "payload": payload,
                    "expected_item_ids": [str(UUID(int=args.candidates))],
                    "forbidden_item_ids": [str(UUID(int=n)) for n in range(1, args.candidates)],
                }
            )
        case_path = output / "cases.json"
        case_path.write_text(json.dumps({"cases": cases}), encoding="utf-8")
        summary = output / "summary.json"
        # Never inherit K6_* overrides that could disable thresholds or export raw samples.
        environment = {
            k: v
            for k, v in os.environ.items()
            if k in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE"}
        }
        environment.update(
            MATCHING_API_URL=matching_url,
            LOSTLINK_ACCESS_TOKEN=owner_token,
            SEARCH_SCOPE="local-fixture",
            SEARCH_PROFILE=args.profile,
            SEARCH_DURATION=args.duration,
            SEARCH_VUS="1",
            SEARCH_WARMUP="2",
            SEARCH_MIN_SAMPLES=str(args.min_samples),
            EXPECTED_CANDIDATE_LIMIT=str(max(20, args.candidates)),
            EXPECTED_MODEL_VERSION=encoder.model_version,
            SEARCH_CASES_FILE=str(case_path),
            SEARCH_SUMMARY_FILE=str(summary),
        )
        completed = subprocess.run(
            [
                str(args.k6),
                "run",
                "--quiet",
                "--no-usage-report",
                str(ROOT / "tests/load/search_review.js"),
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            # k6 caps measurement at 1h, setup at 5m and graceful stop at 30s.
            timeout=4000,
        )
        if not summary.is_file():
            raise RuntimeError("k6 did not produce a summary")
        report = json.loads(summary.read_text(encoding="utf-8"))
        report["local_fixture"] = {
            "actual_candidate_rows": args.candidates,
            "injected_item_delay_ms": args.item_delay_ms,
            "encoder": encoder.model_version,
            "device": "cpu",
            "identity": "injected random test tokens",
            "transport": "loopback HTTP via injected Item transport; no TLS/Cognito",
            "data": (
                "synthetic equal CLIP vectors; all but last Item withdrawn; "
                "direct seed, no ingestion"
            ),
            "diagnostics": diagnostics(rows),
        }
        measured = report["local_fixture"]["diagnostics"]["measure"]
        calls = measured.get("item_calls")
        report["local_fixture"]["expected_call_count_observed"] = bool(
            calls and calls["min"] == calls["max"] == args.candidates
        )
        if not report["local_fixture"]["expected_call_count_observed"]:
            report["passed"] = False
        summary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))
        return completed.returncode or (0 if report["passed"] else 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--k6",
        type=Path,
        default=shutil.which("k6")
        or ROOT / "build/tools/k6" / ("k6.exe" if os.name == "nt" else "k6"),
    )
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--candidates", type=int, choices=[1, 20, 100], default=20)
    parser.add_argument("--item-delay-ms", type=int, default=0)
    parser.add_argument(
        "--modes", choices=["text", "photo", "fused", "text,photo,fused"], default="text"
    )
    parser.add_argument("--profile", choices=["measurement", "validation"], default="measurement")
    parser.add_argument("--duration", default="60s")
    parser.add_argument("--min-samples", type=int, default=100)
    parser.add_argument("--output", type=Path, default=ROOT / "build/search-review" / uuid4().hex)
    args = parser.parse_args()
    if not args.k6.is_file() or not args.checkpoint.is_file() or not 0 <= args.item_delay_ms <= 100:
        parser.error("Check executable/checkpoint paths and delay (0–100 ms)")
    # Unique output prevents stale summaries from being interpreted as a new run.
    if args.output.exists():
        parser.error("Choose a new output directory for each run")
    try:
        return run(args)
    except Exception as exc:
        # SQLAlchemy/HTTP exception strings may contain credentials, IDs or URLs.
        print(
            f"Local diagnostic failed ({type(exc).__name__}); no acceptance result", file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
