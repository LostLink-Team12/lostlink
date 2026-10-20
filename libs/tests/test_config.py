import pytest
from lostlink_common.config import Settings
from lostlink_common.db import identifier
from pydantic import ValidationError


@pytest.mark.parametrize(
    "overrides",
    [
        {"environment": "prod"},
        {"environment": "dev"},
        {"cognito_issuer": "http://untrusted.example.com", "cognito_client_ids": ["client"]},
        {"cognito_issuer": "https://example.com"},
        {"cognito_client_ids": [""]},
        {"database_url": "sqlite://"},
        {"service_name": "../other"},
        {"db_pool_size": 0},
    ],
)
def test_invalid_configuration_is_rejected(overrides):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


def test_database_password_is_not_in_settings_representation():
    settings = Settings(
        _env_file=None, database_url="postgresql+psycopg://app:secret-password@localhost/db"
    )
    assert "secret-password" not in repr(settings)


@pytest.mark.parametrize("value", ["a; DROP TABLE b", "public.items", "", '"quoted"', "A"])
def test_identifiers_reject_sql_syntax(value):
    with pytest.raises(ValueError):
        identifier(value)
