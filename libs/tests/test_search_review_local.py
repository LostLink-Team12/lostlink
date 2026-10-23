"""Reject local-fixture URL overrides before opening the administrator connection."""

import pytest

from scripts import backend_admin, run_search_review_local


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://admin:private-password@remote.example/lostlink",
        "postgresql+psycopg://admin:private-password@localhost/lostlink?hostaddr=192.0.2.1",
        "postgresql+psycopg://admin:private-password@localhost/lostlink?service=other-target",
        "postgresql+psycopg://admin:private-password@localhost/lostlink?user=other-role",
        "postgresql+psycopg://admin:private-password@localhost/lostlink?options=-crole=other",
    ],
)
def test_local_search_fixture_cannot_override_its_target_or_identity(monkeypatch, url):
    monkeypatch.setenv("LOSTLINK_TEST_ADMIN_URL", url)
    monkeypatch.setattr(
        backend_admin, "connect", lambda _: pytest.fail("Must reject before any connection")
    )
    with (
        pytest.raises((ValueError, backend_admin.ToolError)),
        run_search_review_local.disposable_database(),
    ):
        pytest.fail("Invalid target must not create fixture resources")
