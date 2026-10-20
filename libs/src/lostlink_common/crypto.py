"""Authenticated field encryption bound to the row and column that stores the value.

AES-256-GCM from the reviewed ``cryptography`` package with a random 96-bit nonce per
value. The associated data binds service, table, column, record ID and key ID, so a
ciphertext copied to another row or column fails to decrypt. Keys come from a secret
store as a JSON object of key ID to base64 key; the active key encrypts new values and
older keys remain available for decryption until data is re-encrypted.
"""

import base64
import binascii
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

FORMAT_VERSION = b"\x01"
NONCE_BYTES = 12
TAG_BYTES = 16
KEY_BYTES = 32
MAX_KEYS = 16
MAX_PLAINTEXT_BYTES = 64 * 1024
KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SERVICE_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,62}")
IDENTIFIER_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,62}")


class EncryptionUnavailable(Exception):
    """Keys are missing or invalid; dependent features must stay unready."""


class DecryptionFailed(Exception):
    """Unknown key, wrong location, tampering or a malformed stored value."""


@dataclass(frozen=True)
class EncryptionContext:
    service: str
    table: str
    column: str
    record_id: UUID

    def __post_init__(self) -> None:
        if not SERVICE_PATTERN.fullmatch(self.service):
            raise ValueError("Encryption service name is invalid")
        if not IDENTIFIER_PATTERN.fullmatch(self.table) or not IDENTIFIER_PATTERN.fullmatch(
            self.column
        ):
            raise ValueError("Encryption table and column must be SQL identifiers")
        if not isinstance(self.record_id, UUID):
            raise ValueError("Encryption record ID must be a UUID")

    def associated_data(self, key_id: str) -> bytes:
        parts = ("lostlink-field", "v1", self.service, self.table, self.column)
        encoded = [part.encode() for part in (*parts, str(self.record_id), key_id)]
        # Length prefixes keep ("a", "bc") and ("ab", "c") distinct.
        return b"".join(len(part).to_bytes(2, "big") + part for part in encoded)


@dataclass(frozen=True)
class EncryptedValue:
    key_id: str
    ciphertext: bytes = field(repr=False)


def _decode_key(key_id: str, encoded: object) -> bytes:
    if not isinstance(key_id, str) or not KEY_ID_PATTERN.fullmatch(key_id):
        raise EncryptionUnavailable("Encryption key IDs are invalid")
    if not isinstance(encoded, str):
        raise EncryptionUnavailable("Encryption keys must be base64 strings")
    try:
        key = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise EncryptionUnavailable("Encryption keys must be base64 strings") from exc
    if len(key) != KEY_BYTES:
        raise EncryptionUnavailable("Encryption keys must be 32 bytes")
    return key


class FieldCipher:
    def __init__(self, keys: Mapping[str, bytes], active_key_id: str):
        if not keys or len(keys) > MAX_KEYS:
            raise EncryptionUnavailable("Configure between one and sixteen encryption keys")
        for key_id, key in keys.items():
            if not KEY_ID_PATTERN.fullmatch(key_id) or len(key) != KEY_BYTES:
                raise EncryptionUnavailable("Encryption keys are invalid")
        if active_key_id not in keys:
            raise EncryptionUnavailable("The active encryption key is not configured")
        self._ciphers = {key_id: AESGCM(bytes(key)) for key_id, key in keys.items()}
        self.active_key_id = active_key_id

    @classmethod
    def from_secret(cls, keys_json: SecretStr | str, active_key_id: str) -> "FieldCipher":
        raw = keys_json.get_secret_value() if isinstance(keys_json, SecretStr) else keys_json
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise EncryptionUnavailable("Encryption keys must be a JSON object") from exc
        if not isinstance(parsed, dict):
            raise EncryptionUnavailable("Encryption keys must be a JSON object")
        return cls(
            {key_id: _decode_key(key_id, value) for key_id, value in parsed.items()}, active_key_id
        )

    @classmethod
    def from_settings(
        cls, keys_json: SecretStr | None, active_key_id: str | None
    ) -> "FieldCipher | None":
        """None when both are absent; a partial or invalid configuration is an error."""
        if keys_json is None and active_key_id is None:
            return None
        if keys_json is None or active_key_id is None:
            raise EncryptionUnavailable("Configure encryption keys and active key ID together")
        return cls.from_secret(keys_json, active_key_id)

    @staticmethod
    def generate_key() -> str:
        return base64.b64encode(os.urandom(KEY_BYTES)).decode()

    def encrypt(self, plaintext: bytes, context: EncryptionContext) -> EncryptedValue:
        if not isinstance(plaintext, bytes) or len(plaintext) > MAX_PLAINTEXT_BYTES:
            raise ValueError("Plaintext must be at most 64 KiB of bytes")
        nonce = os.urandom(NONCE_BYTES)
        sealed = self._ciphers[self.active_key_id].encrypt(
            nonce, plaintext, context.associated_data(self.active_key_id)
        )
        return EncryptedValue(self.active_key_id, FORMAT_VERSION + nonce + sealed)

    def decrypt(self, value: EncryptedValue, context: EncryptionContext) -> bytes:
        cipher = self._ciphers.get(value.key_id)
        blob = value.ciphertext
        if (
            cipher is None
            or not isinstance(blob, bytes)
            or len(blob) < 1 + NONCE_BYTES + TAG_BYTES
            or blob[:1] != FORMAT_VERSION
        ):
            raise DecryptionFailed("Stored value cannot be decrypted")
        nonce, sealed = blob[1 : 1 + NONCE_BYTES], blob[1 + NONCE_BYTES :]
        try:
            return cipher.decrypt(nonce, sealed, context.associated_data(value.key_id))
        except InvalidTag as exc:
            raise DecryptionFailed("Stored value cannot be decrypted") from exc

    def encrypt_text(self, plaintext: str, context: EncryptionContext) -> EncryptedValue:
        return self.encrypt(plaintext.encode("utf-8"), context)

    def decrypt_text(self, value: EncryptedValue, context: EncryptionContext) -> str:
        try:
            return self.decrypt(value, context).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DecryptionFailed("Stored value cannot be decrypted") from exc

    def needs_rotation(self, value: EncryptedValue) -> bool:
        return value.key_id != self.active_key_id

    def __repr__(self) -> str:
        return f"FieldCipher(active_key_id={self.active_key_id!r}, keys={len(self._ciphers)})"
