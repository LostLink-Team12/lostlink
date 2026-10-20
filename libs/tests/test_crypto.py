import base64
import json
from uuid import uuid4

import pytest
from lostlink_common.crypto import (
    DecryptionFailed,
    EncryptedValue,
    EncryptionContext,
    EncryptionUnavailable,
    FieldCipher,
)
from pydantic import SecretStr


def keyring(**keys):
    return SecretStr(json.dumps(keys))


@pytest.fixture
def context():
    return EncryptionContext("claim-service", "claim_contacts", "encrypted_contact", uuid4())


def test_roundtrip_uses_random_nonces_and_never_reveals_keys(context):
    key = FieldCipher.generate_key()
    cipher = FieldCipher.from_secret(keyring(k1=key), "k1")
    first = cipher.encrypt_text("+65 8000 0000", context)
    second = cipher.encrypt_text("+65 8000 0000", context)
    assert first.ciphertext != second.ciphertext
    assert cipher.decrypt_text(first, context) == "+65 8000 0000"
    assert b"8000" not in first.ciphertext
    assert key not in repr(cipher) and "8000" not in repr(first)


@pytest.mark.parametrize("field", ["service", "table", "column", "record_id"])
def test_ciphertext_is_bound_to_its_storage_location(context, field):
    cipher = FieldCipher({"k1": bytes(32)}, "k1")
    value = cipher.encrypt(b"secret", context)
    moved = {
        "service": "notification-service",
        "table": "other_table",
        "column": "other_column",
        "record_id": uuid4(),
    }
    other = EncryptionContext(**{**context.__dict__, field: moved[field]})
    with pytest.raises(DecryptionFailed):
        cipher.decrypt(value, other)


def test_tampering_unknown_keys_and_relabelled_keys_fail_closed(context):
    cipher = FieldCipher({"k1": bytes(32), "k2": bytes([1]) * 32}, "k1")
    value = cipher.encrypt(b"secret", context)
    tampered = bytearray(value.ciphertext)
    tampered[-1] ^= 1
    for broken in [
        EncryptedValue("k1", bytes(tampered)),
        EncryptedValue("k1", value.ciphertext[:20]),
        EncryptedValue("k1", b"\x02" + value.ciphertext[1:]),
        EncryptedValue("missing", value.ciphertext),
        EncryptedValue("k2", value.ciphertext),
    ]:
        with pytest.raises(DecryptionFailed):
            cipher.decrypt(broken, context)


def test_rotation_keeps_old_values_readable(context):
    old_key, new_key = FieldCipher.generate_key(), FieldCipher.generate_key()
    old = FieldCipher.from_secret(keyring(k1=old_key), "k1")
    value = old.encrypt(b"contact", context)
    rotated = FieldCipher.from_secret(keyring(k1=old_key, k2=new_key), "k2")
    assert rotated.needs_rotation(value)
    assert rotated.decrypt(value, context) == b"contact"
    fresh = rotated.encrypt(b"contact", context)
    assert fresh.key_id == "k2" and not rotated.needs_rotation(fresh)


@pytest.mark.parametrize(
    ("keys", "active"),
    [
        ("not json", "k1"),
        ("[]", "k1"),
        ("{}", "k1"),
        (json.dumps({"k1": base64.b64encode(bytes(16)).decode()}), "k1"),
        (json.dumps({"k1": "***"}), "k1"),
        (json.dumps({"bad id": base64.b64encode(bytes(32)).decode()}), "bad id"),
        (json.dumps({"k1": base64.b64encode(bytes(32)).decode()}), "k2"),
    ],
)
def test_invalid_key_configuration_is_rejected(keys, active):
    with pytest.raises(EncryptionUnavailable):
        FieldCipher.from_secret(SecretStr(keys), active)


def test_partial_settings_are_rejected_and_absent_settings_disable_the_feature():
    assert FieldCipher.from_settings(None, None) is None
    with pytest.raises(EncryptionUnavailable):
        FieldCipher.from_settings(None, "k1")
    with pytest.raises(EncryptionUnavailable):
        FieldCipher.from_settings(keyring(k1=FieldCipher.generate_key()), None)


def test_context_and_plaintext_limits():
    with pytest.raises(ValueError):
        EncryptionContext("Claim Service", "claims", "contact", uuid4())
    with pytest.raises(ValueError):
        EncryptionContext("claim-service", "claims;drop", "contact", uuid4())
    with pytest.raises(ValueError):
        EncryptionContext("claim-service", "claims", "contact", "not-a-uuid")
    cipher = FieldCipher({"k1": bytes(32)}, "k1")
    context = EncryptionContext("claim-service", "claims", "contact", uuid4())
    with pytest.raises(ValueError):
        cipher.encrypt(b"x" * (64 * 1024 + 1), context)
    with pytest.raises(ValueError):
        cipher.encrypt("text", context)
