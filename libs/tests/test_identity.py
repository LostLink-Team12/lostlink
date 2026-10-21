import boto3
import pytest
from botocore.exceptions import ConnectTimeoutError
from botocore.stub import Stubber
from lostlink_common.identity import (
    CognitoIdentityDirectory,
    EmailNotVerified,
    IdentityDirectoryUnavailable,
    IdentityNotFound,
    StaticIdentityDirectory,
)

POOL = "ap-southeast-1_Example1"
SUBJECT = "11111111-2222-3333-4444-555555555555"


def cognito():
    return boto3.client(
        "cognito-idp",
        region_name="ap-southeast-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )


def user(subject=SUBJECT, email="owner@example.com", verified="true", enabled=True):
    attributes = [{"Name": "sub", "Value": subject}]
    if email is not None:
        attributes.append({"Name": "email", "Value": email})
    if verified is not None:
        attributes.append({"Name": "email_verified", "Value": verified})
    return {"Username": f"user-{subject}", "Attributes": attributes, "Enabled": enabled}


def expected_request(subject=SUBJECT):
    return {
        "UserPoolId": POOL,
        "Filter": f'sub = "{subject}"',
        "Limit": 2,
        "AttributesToGet": ["sub", "email", "email_verified"],
    }


def test_exact_subject_returns_only_a_verified_email():
    client = cognito()
    with Stubber(client) as stub:
        stub.add_response("list_users", {"Users": [user()]}, expected_request())
        result = CognitoIdentityDirectory(client, POOL).resolve_verified_email(SUBJECT)
        stub.assert_no_pending_responses()
    assert (result.subject, result.email) == (SUBJECT, "owner@example.com")
    assert "owner@example.com" not in repr(result)


def test_filter_quotes_are_escaped_and_partial_matches_are_ignored():
    client = cognito()
    subject = 'a"b\\c'
    with Stubber(client) as stub:
        stub.add_response(
            "list_users",
            {"Users": [user(subject="a")]},
            {**expected_request(), "Filter": 'sub = "a\\"b\\\\c"'},
        )
        with pytest.raises(IdentityNotFound):
            CognitoIdentityDirectory(client, POOL).resolve_verified_email(subject)


@pytest.mark.parametrize(
    ("users", "error"),
    [
        ([], IdentityNotFound),
        ([user(), user()], IdentityNotFound),
        ([user(enabled=False)], IdentityNotFound),
        ([user(verified="false")], EmailNotVerified),
        ([user(verified=None)], EmailNotVerified),
        ([user(email=None)], EmailNotVerified),
        ([user(email="not an email")], EmailNotVerified),
    ],
)
def test_missing_ambiguous_disabled_or_unverified_accounts_are_explicit(users, error):
    client = cognito()
    with Stubber(client) as stub:
        stub.add_response("list_users", {"Users": users}, expected_request())
        with pytest.raises(error):
            CognitoIdentityDirectory(client, POOL).resolve_verified_email(SUBJECT)


def test_pagination_is_bounded_and_provider_errors_are_retryable():
    client = cognito()
    with Stubber(client) as stub:
        for _ in range(3):
            stub.add_response("list_users", {"Users": [], "PaginationToken": "next"})
        with pytest.raises(IdentityDirectoryUnavailable):
            CognitoIdentityDirectory(client, POOL).resolve_verified_email(SUBJECT)
        stub.add_client_error("list_users", service_error_code="TooManyRequestsException")
        with pytest.raises(IdentityDirectoryUnavailable):
            CognitoIdentityDirectory(client, POOL).resolve_verified_email(SUBJECT)

    class Timeout:
        def list_users(self, **_):
            raise ConnectTimeoutError(endpoint_url="https://cognito-idp.invalid")

    with pytest.raises(IdentityDirectoryUnavailable):
        CognitoIdentityDirectory(Timeout(), POOL).resolve_verified_email(SUBJECT)


@pytest.mark.parametrize("subject", ["", "x" * 129, "line\nbreak"])
def test_invalid_subjects_never_reach_the_provider(subject):
    class Unexpected:
        def list_users(self, **_):
            raise AssertionError("provider must not be called")

    with pytest.raises(ValueError):
        CognitoIdentityDirectory(Unexpected(), POOL).resolve_verified_email(subject)


def test_static_double_mirrors_adapter_outcomes():
    directory = StaticIdentityDirectory(
        {"verified": ("a@example.com", True), "unverified": ("b@example.com", False)}
    )
    assert directory.resolve_verified_email("verified").email == "a@example.com"
    with pytest.raises(EmailNotVerified):
        directory.resolve_verified_email("unverified")
    with pytest.raises(IdentityNotFound):
        directory.resolve_verified_email("missing")
    with pytest.raises(IdentityDirectoryUnavailable):
        StaticIdentityDirectory({}, available=False).resolve_verified_email("verified")
    with pytest.raises(ValueError):
        CognitoIdentityDirectory(object(), "")
