"""Export service OpenAPI documents and event schemas into docs/api.

Usage from the repository root:

    python scripts/export_contracts.py          # rewrite docs/api
    python scripts/export_contracts.py --check  # exit 1 if docs/api is stale
"""

import argparse
import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "api"
SERVICES = {
    "venue-service": ("venue_app.main", "venue_app.config", "VenueSettings"),
    "item-service": ("item_app.main", "item_app.config", "ItemSettings"),
    "matching-service": ("matching_app.main", "matching_app.config", "MatchingSettings"),
    "claim-service": ("claim_app.main", "claim_app.config", "ClaimSettings"),
    "notification-service": (
        "notification_app.main",
        "notification_app.config",
        "NotificationSettings",
    ),
}


def render(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def service_documents():
    sys.path.insert(0, str(ROOT / "libs" / "src"))
    for service in SERVICES:
        sys.path.insert(0, str(ROOT / "services" / service))
    documents = {}
    for service, (main_module, config_module, settings_class) in SERVICES.items():
        settings = getattr(importlib.import_module(config_module), settings_class)(_env_file=None)
        app = importlib.import_module(main_module).create_app(settings)
        documents[f"{service}.openapi.json"] = app.openapi()
    return documents


def event_document():
    from lostlink_common.event_schemas import EVENT_SCHEMAS
    from lostlink_common.events import DomainEvent

    return {
        "title": "LostLink domain events",
        "description": (
            "Version-one envelopes published on SNS. Payloads reject unknown fields; "
            "consumers deduplicate by event_id."
        ),
        "envelope": DomainEvent.model_json_schema(),
        "events": {
            event_type: {
                "source": source,
                "tenant_required": tenant_required,
                "data": payload.model_json_schema(),
            }
            for event_type, (payload, source, tenant_required) in sorted(EVENT_SCHEMAS.items())
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when docs/api is stale")
    args = parser.parse_args()
    files = {name: render(value) for name, value in service_documents().items()}
    files["events.schema.json"] = render(event_document())
    stale = [
        name
        for name, content in files.items()
        if not (OUTPUT / name).exists() or (OUTPUT / name).read_text(encoding="utf-8") != content
    ]
    if args.check:
        if stale:
            print("Stale API contracts: " + ", ".join(stale))
            return 1
        return 0
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (OUTPUT / name).write_text(content, encoding="utf-8", newline="\n")
    print(f"Wrote {len(files)} contract files to {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
