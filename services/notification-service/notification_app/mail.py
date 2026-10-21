"""match-found-v1 rendering and email senders with explicit not-sent/unknown outcomes."""

import html
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
)

TEMPLATE_VERSION = "match-found-v1"
SUBJECT = "LostLink: a possible match for your lost item"
NOT_SENT_CODES = {
    "MessageRejected": "message_rejected",
    "MailFromDomainNotVerifiedException": "sender_not_verified",
    "MailFromDomainNotVerified": "sender_not_verified",
    "ConfigurationSetDoesNotExist": "configuration_invalid",
    "ConfigurationSetSendingPausedException": "sending_paused",
    "AccountSendingPausedException": "sending_paused",
}
THROTTLED = {"Throttling", "ThrottlingException", "TooManyRequestsException"}


class SendNotAccepted(Exception):
    """The provider certainly did not accept the message."""

    def __init__(self, code: str, retryable: bool):
        super().__init__(code)
        self.code, self.retryable = code, retryable


class SendOutcomeUnknown(Exception):
    """The provider may have accepted the message; never resend automatically."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EmailMessage:
    to: str = field(repr=False)
    subject: str
    text: str = field(repr=False)
    html: str = field(repr=False)


def render_match_found(
    *, to: str, match_id: UUID, item_title: str | None, venue_name: str | None, base_url: str
) -> EmailMessage:
    # The link uses a configured origin and a UUID; event data never chooses the domain.
    link = f"{base_url.rstrip('/')}/matches/{UUID(str(match_id))}"
    title = item_title or "an item"
    venue = venue_name or "a LostLink venue"
    text = (
        "Hello,\n\n"
        f'A found item, "{title}", at {venue} may match your lost item report.\n'
        f"Review the match in the LostLink app: {link}\n\n"
        "A match is not proof of ownership. To collect the item, submit a claim and answer\n"
        "the venue's verification questions.\n\n"
        "You received this email because you reported a lost item on LostLink.\n"
    )
    body = (
        "<!doctype html><html><body>"
        "<p>Hello,</p>"
        f"<p>A found item, &quot;{html.escape(title)}&quot;, at {html.escape(venue)} "
        "may match your lost item report.</p>"
        f'<p><a href="{html.escape(link, quote=True)}">Review the match in LostLink</a></p>'
        "<p>A match is not proof of ownership. To collect the item, submit a claim and "
        "answer the venue's verification questions.</p>"
        "<p>You received this email because you reported a lost item on LostLink.</p>"
        "</body></html>"
    )
    return EmailMessage(to=to, subject=SUBJECT, text=text, html=body)


def ses_client(settings):
    options: dict[str, Any] = {}
    if settings.aws_endpoint_url:
        options.update(
            endpoint_url=settings.aws_endpoint_url,
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
    # One attempt only: an SDK retry after a lost response could send a duplicate email.
    return boto3.client(
        "ses",
        region_name=settings.aws_region,
        config=Config(connect_timeout=2, read_timeout=5, retries={"total_max_attempts": 1}),
        **options,
    )


class SesSender:
    def __init__(self, client, from_address: str, configuration_set: str | None = None):
        if not from_address:
            raise ValueError("A verified SES sender address is required")
        self.client, self.from_address, self.configuration_set = (
            client,
            from_address,
            configuration_set,
        )

    def send(self, message: EmailMessage) -> str:
        request = {
            "Source": self.from_address,
            "Destination": {"ToAddresses": [message.to]},
            "Message": {
                "Subject": {"Data": message.subject, "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": message.text, "Charset": "UTF-8"},
                    "Html": {"Data": message.html, "Charset": "UTF-8"},
                },
            },
        }
        if self.configuration_set:
            request["ConfigurationSetName"] = self.configuration_set
        try:
            response = self.client.send_email(**request)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or 0
            if code in THROTTLED:
                raise SendNotAccepted("provider_throttled", True) from exc
            if code in NOT_SENT_CODES:
                raise SendNotAccepted(NOT_SENT_CODES[code], False) from exc
            if status >= 500:
                raise SendOutcomeUnknown("provider_error") from exc
            raise SendNotAccepted("provider_rejected", False) from exc
        except (EndpointConnectionError, ConnectTimeoutError) as exc:
            # No connection was established, so the request never reached SES.
            raise SendNotAccepted("connection_failed", True) from exc
        except BotoCoreError as exc:
            raise SendOutcomeUnknown("provider_timeout") from exc
        message_id = response.get("MessageId")
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 256:
            raise SendOutcomeUnknown("invalid_provider_response")
        return message_id


class CaptureSender:
    """Local/test sender. outcomes may hold 'not_sent', 'throttled' or 'unknown'."""

    def __init__(self):
        self.messages: list[EmailMessage] = []
        self.outcomes: list[str] = []

    def send(self, message: EmailMessage) -> str:
        outcome = self.outcomes.pop(0) if self.outcomes else "sent"
        if outcome == "not_sent":
            raise SendNotAccepted("message_rejected", False)
        if outcome == "throttled":
            raise SendNotAccepted("provider_throttled", True)
        if outcome == "unknown":
            self.messages.append(message)
            raise SendOutcomeUnknown("provider_timeout")
        self.messages.append(message)
        return f"capture-{len(self.messages)}"


def make_sender(settings, client=None):
    if settings.email_sender == "capture":
        return CaptureSender()
    if not settings.ses_from_address:
        return None
    return SesSender(
        client or ses_client(settings), settings.ses_from_address, settings.ses_configuration_set
    )
