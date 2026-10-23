"""Offline deployment input/key validation, including non-sensitive failure output."""

import base64
import json

import pytest

from scripts import backend_admin as tool


def configure_keys(monkeypatch, **changes):
    values = {
        "FIELD_ENCRYPTION_KEYS": json.dumps(
            {
                "old": base64.b64encode(b"a" * 32).decode(),
                "current": base64.b64encode(b"b" * 32).decode(),
            }
        ),
        "FIELD_ENCRYPTION_ACTIVE_KEY_ID": "current",
        "HANDOVER_TOKEN_KEY": base64.b64encode(b"c" * 32).decode(),
    }
    values.update(changes)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def test_key_check_reads_existing_rotation_ring_and_does_not_print_or_change_secrets(
    monkeypatch, capsys
):
    values = configure_keys(monkeypatch)
    for _ in range(2):
        assert tool.main(["keys"]) == 0
        result = json.loads(capsys.readouterr().out)
        assert result["field_keys"] == 2 and result["active_key_present"]
        assert not any(value in json.dumps(result) for value in values.values())
    assert all(tool.os.environ[name] == value for name, value in values.items())


@pytest.mark.parametrize(
    "changes",
    [
        {"FIELD_ENCRYPTION_KEYS": "private-invalid-input"},
        {
            "FIELD_ENCRYPTION_KEYS": '{"current":"'
            + base64.b64encode(b"a" * 32).decode()
            + '","current":"'
            + base64.b64encode(b"b" * 32).decode()
            + '"}'
        },
        {"FIELD_ENCRYPTION_ACTIVE_KEY_ID": "missing"},
        {"FIELD_ENCRYPTION_KEYS": json.dumps({"current": "YQ=="})},
        {"HANDOVER_TOKEN_KEY": "private-invalid-input"},
        {"HANDOVER_TOKEN_KEY": base64.b64encode(b"short").decode()},
        {"HANDOVER_TOKEN_KEY": base64.b64encode(b"b" * 32).decode()},
        {
            "FIELD_ENCRYPTION_KEYS": json.dumps(
                {
                    "old": base64.b64encode(b"b" * 32).decode(),
                    "current": base64.b64encode(b"b" * 32).decode(),
                }
            )
        },
    ],
)
def test_bad_key_configuration_fails_without_echoing_values(monkeypatch, capsys, changes):
    values = configure_keys(monkeypatch, **changes)
    assert tool.main(["keys"]) == 1
    result = capsys.readouterr()
    assert json.loads(result.out)["status"] == "failed" and not result.err
    assert "private-invalid-input" not in result.out
    assert values["FIELD_ENCRYPTION_KEYS"] not in result.out
    assert values["HANDOVER_TOKEN_KEY"] not in result.out


def test_plan_needs_no_credentials_and_rejects_colliding_roles(tmp_path, capsys):
    assert tool.main(["plan"]) == 0
    assert len(json.loads(capsys.readouterr().out)["services"]) == 5
    document = json.loads((tool.ROOT / "infra/database/backend.example.json").read_text())
    document["services"]["venue"]["roles"]["identity"]["name"] = "lostlink_venue_api"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(document))
    assert tool.main(["plan", "--config", str(path)]) == 1
    assert "runtime_roles_must_be_distinct" in capsys.readouterr().out


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///private-invalid-input",
        "postgresql+psycopg://admin:private-invalid-input@localhost/other",
        "postgresql+psycopg://admin:private-invalid-input@localhost/lostlink",
        "postgresql+psycopg://admin:private-invalid-input@localhost/lostlink?user=another",
    ],
)
def test_deployed_connection_inputs_fail_before_network_access(monkeypatch, capsys, url):
    monkeypatch.setenv("VENUE_MIGRATION_DATABASE_URL", url)
    assert tool.main(["migrate", "--service", "venue"]) == 1
    output = capsys.readouterr().out
    assert "private-invalid-input" not in output and "localhost" not in output


def test_unknown_json_fields_are_rejected_without_echoing_secret_values(tmp_path, capsys):
    config = json.loads((tool.ROOT / "infra/database/backend.example.json").read_text())
    config["password"] = "private-invalid-input"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    assert tool.main(["plan", "--config", str(path)]) == 1
    assert "private-invalid-input" not in capsys.readouterr().out


@pytest.mark.parametrize("reuse_migration_password", [False, True])
def test_reused_database_secrets_fail_before_any_connection(
    monkeypatch, capsys, reuse_migration_password
):
    config = tool.load(tool.ROOT / "infra/database/backend.example.json")
    monkeypatch.setenv(
        "VENUE_MIGRATION_DATABASE_URL",
        "postgresql+psycopg://operator:private-migration-secret@localhost/lostlink",
    )
    for index, role in enumerate(config.services["venue"].roles.values()):
        monkeypatch.setenv(role.password_env, f"private-runtime-secret-{index}")
    target = config.services["venue"].roles["identity"]
    duplicate = (
        "private-migration-secret" if reuse_migration_password else "private-runtime-secret-0"
    )
    monkeypatch.setenv(target.password_env, duplicate)
    monkeypatch.setattr(tool, "connect", lambda _: pytest.fail("No connection before preflight"))
    assert tool.main(["migrate", "--service", "venue", "--environment", "local"]) == 1
    output = capsys.readouterr().out
    assert "database_password_reused" in output
    assert duplicate not in output
