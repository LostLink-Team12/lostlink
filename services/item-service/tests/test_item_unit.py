from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from item_app.config import ItemSettings
from item_app.main import create_app
from item_app.schemas import ImportRequest, ItemChange, NewItem, UploadRequest
from item_app.storage import PhotoStorage, StorageError
from lostlink_common.events import DomainEvent
from pydantic import ValidationError


@pytest.mark.parametrize("internal_pool,expected", [(None, 1), (2, 2)])
def test_internal_pool_can_grow_without_growing_the_user_pool(monkeypatch, internal_pool, expected):
    opened, closed = [], []

    class DatabaseSpy:
        def __init__(self, settings):
            self.settings = settings
            opened.append(settings)

        def close(self):
            closed.append(self.settings)

    monkeypatch.setattr("item_app.main.ItemDatabase", DatabaseSpy)
    api_url = "postgresql+psycopg://item_api:pw@localhost/lostlink"
    internal_url = "postgresql+psycopg://item_internal:pw@localhost/lostlink"
    settings = ItemSettings(
        _env_file=None,
        database_url=api_url,
        internal_database_url=internal_url,
        db_pool_size=1,
        db_max_overflow=0,
        internal_db_pool_size=internal_pool,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200
        assert [s.database_url.get_secret_value() for s in opened] == [api_url, internal_url]
        assert [s.db_pool_size for s in opened] == [1, expected]
        assert all(s.db_max_overflow == 0 for s in opened)
        assert settings.db_pool_size == 1
    assert len(closed) == 2


def valid_item():
    return {
        "title": "Blue umbrella",
        "description": "A blue umbrella",
        "category": "umbrella",
        "found_at": datetime.now(UTC).isoformat(),
        "verification_questions": [
            {"prompt": "What is written inside?", "reference_answer": "Hidden initials"}
        ],
    }


@pytest.mark.parametrize(
    "change",
    [
        {"title": ""},
        {"found_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        {"found_at": "2026-10-20T12:00:00"},
        {"verification_questions": []},
        {"owner_id": "forged"},
        {"tenant_id": str(uuid4())},
        {"description": "x" * 2001},
    ],
)
def test_item_inputs_reject_invalid_time_bounds_and_system_fields(change):
    with pytest.raises(ValidationError):
        NewItem.model_validate({**valid_item(), **change})


@pytest.mark.parametrize("body", [{}, {"title": None}, {"state": "available"}])
def test_patch_does_not_allow_nulls_empty_changes_or_system_state(body):
    with pytest.raises(ValidationError):
        ItemChange.model_validate(body)


@pytest.mark.parametrize(
    "body",
    [
        {"content_type": "image/svg+xml", "size": 100},
        {"content_type": "image/jpeg", "size": 10485761},
        {"content_type": "image/png", "size": "100"},
        {"content_type": "image/png", "size": 10, "key": "sealed/other"},
    ],
)
def test_upload_bounds_and_caller_keys_are_rejected(body):
    with pytest.raises(ValidationError):
        UploadRequest.model_validate(body)


def test_import_validates_envelope_and_leaves_row_errors_independent():
    row = {"client_reference": "one", "photo_url": "http://untrusted.invalid/image"}
    assert ImportRequest(batch_reference="batch", entries=[row]).entries == [row]
    for entries in [[row] * 101, [row, row], [{"title": "missing reference"}]]:
        with pytest.raises(ValidationError):
            ImportRequest(batch_reference="batch", entries=entries)


def test_unconfigured_probes_do_not_claim_ready_or_allow_unauthenticated_access():
    with TestClient(create_app(ItemSettings(_env_file=None))) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/ready").status_code == 503
        assert client.get("/v1/items").status_code == 401
        assert "/v1/item-imports" in client.get("/openapi.json").json()["paths"]


def test_role_and_client_configurations_are_separate():
    for options in [
        {"user_client_ids": ["same"], "claim_client_ids": ["same"]},
        {
            "database_url": "postgresql+psycopg://same:p@localhost/db",
            "internal_database_url": "postgresql+psycopg://same:q@localhost/db",
        },
        {"venue_base_url": "http://untrusted.invalid"},
    ]:
        with pytest.raises(ValueError):
            ItemSettings(_env_file=None, **options)
    settings = ItemSettings(
        _env_file=None,
        environment="prod",
        process_mode="worker",
        worker_database_url="postgresql+psycopg://worker:p@localhost/db",
        photo_bucket="photos",
    )
    assert settings.database_url is None


def test_embedding_failure_schema_and_catalog_do_not_allow_private_answers():
    event = DomainEvent(
        event_type="item.embedding_failed",
        source="embedding-worker",
        tenant_id=uuid4(),
        data={"item_id": str(uuid4()), "item_version": 1, "error_code": "invalid_image"},
    )
    assert DomainEvent.model_validate_json(event.to_message()) == event
    with pytest.raises(ValueError):
        DomainEvent.model_validate(
            {**event.model_dump(), "data": {**event.data, "reference_answer": "secret"}}
        )
    with pytest.raises(ValueError):
        DomainEvent.model_validate(
            {**event.model_dump(), "data": {**event.data, "error_code": "raw stack"}}
        )


def photo():
    return SimpleNamespace(
        id=uuid4(),
        item_version=1,
        bucket="photos",
        size=3,
        content_type="image/png",
        staging_key="staging/test",
        sealed_key="sealed/test",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )


def missing():
    return ClientError({"Error": {"Code": "404"}}, "HeadObject")


def test_presigned_post_is_exact_key_type_size_and_fixed_expiry():
    class S3:
        def generate_presigned_post(self, **kwargs):
            assert kwargs["Key"] == "staging/test"
            assert kwargs["Conditions"] == [
                {"Content-Type": "image/png"},
                ["content-length-range", 3, 3],
            ]
            assert 1 <= kwargs["ExpiresIn"] <= 600
            return {"url": "https://s3.invalid"}

    assert PhotoStorage(S3()).presign(photo())["url"] == "https://s3.invalid"


def test_source_mutation_aborts_sealing_and_never_writes_destination():
    class S3:
        def head_object(self, **kwargs):
            if kwargs["Key"] == "sealed/test":
                raise missing()
            return {"ContentLength": 3, "ContentType": "image/png", "ETag": '"source"'}

        def get_object(self, **kwargs):
            assert kwargs["IfMatch"] == '"source"'
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "GetObject")

    with pytest.raises(StorageError) as exc:
        PhotoStorage(S3()).seal(photo())
    assert exc.value.code == "photo_changed_during_seal" and exc.value.retryable


def test_existing_sealed_snapshot_is_reused_without_reading_replaced_staging():
    value = photo()

    class S3:
        def head_object(self, **kwargs):
            assert kwargs["Key"] == "sealed/test"
            return {
                "ContentLength": 3,
                "ContentType": "image/png",
                "ETag": '"sealed"',
                "Metadata": {"upload-id": str(value.id), "item-version": "1", "sha256": "a" * 64},
            }

    assert PhotoStorage(S3()).seal(value) == '"sealed"'
