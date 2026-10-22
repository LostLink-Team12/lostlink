"""Real TLS verifies trust/hostname while Item state stays fresh across reads."""

import json
import ssl
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from uuid import uuid4

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from lostlink_common.membership import MembershipUnavailable
from matching_app.clients import ItemClient


def test_verified_context_is_reused_but_state_and_authorization_are_fetched_every_time(
    tmp_path, monkeypatch
):
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "C3 synthetic test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    root, certificate, private = (tmp_path / name for name in ["ca.pem", "server.pem", "key.pem"])
    root.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    monkeypatch.setenv("SSL_CERT_FILE", str(root))
    empty_roots = tmp_path / "empty-roots"
    empty_roots.mkdir()
    monkeypatch.setenv("SSL_CERT_DIR", str(empty_roots))
    item_id, tenant_id, correlation = uuid4(), uuid4(), uuid4()
    authorizations = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            authorizations.append(self.headers["Authorization"])
            value = {
                "item_id": str(item_id),
                "tenant_id": str(tenant_id),
                "item_version": 1,
                "revision": len(authorizations),
                "state": "available" if len(authorizations) == 1 else "reserved",
                "title": "Bag",
                "description": "Test bag",
                "category": "Backpack",
                "found_at": now.isoformat(),
                "processing_correlation_id": str(correlation),
                "photo_bucket": "demo",
                "photo_key": "sealed/test",
            }
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certificate, private)
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    contexts = []
    original = ssl.create_default_context

    def create_context():
        context = original()
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        contexts.append(context)
        return context

    monkeypatch.setattr(ssl, "create_default_context", create_context)
    try:
        tokens = iter(["first-token", "current-token"])
        client = ItemClient(f"https://localhost:{server.server_port}", lambda: next(tokens))
        assert client.context(item_id).state == "available"
        assert client.context(item_id).state == "reserved"
        assert len(contexts) == 1
        assert authorizations == ["Bearer first-token", "Bearer current-token"]
        wrong_host = ItemClient(f"https://127.0.0.1:{server.server_port}", lambda: "token")
        with pytest.raises(MembershipUnavailable):
            wrong_host.context(item_id)
        # A separate trust store has no copy of the private test CA.
        monkeypatch.setenv("SSL_CERT_FILE", str(empty_roots / "absent.pem"))
        untrusted = ItemClient(f"https://localhost:{server.server_port}", lambda: "token")
        with pytest.raises(MembershipUnavailable):
            untrusted.context(item_id)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
