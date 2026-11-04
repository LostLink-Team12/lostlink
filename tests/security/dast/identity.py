"""Ephemeral JWKS server, accessible only inside the disposable DAST network."""

import base64
import json
import ssl
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import jwt
from jwt.algorithms import RSAAlgorithm


def mint(subject, client="dast-owner", roles=("owner",), tenant=None, *, overrides=None, key=None):
    now = int(time.time())
    claims = {
        "iss": "https://identity:8443",
        "sub": subject,
        "client_id": client,
        "aud": "dast-api",
        "token_use": "access",
        "iat": now,
        "exp": now + 900,
        "cognito:groups": list(roles),
    }
    if tenant:
        claims["custom:tenant_id"] = tenant
    claims.update(overrides or {})
    key = key or RSAAlgorithm.from_jwk(Path("/authority/jwk.json").read_text())
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "dast"})


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path != "/.well-known/jwks.json":
            self.send_error(404)
            return
        key = RSAAlgorithm.from_jwk(Path("/authority/jwk.json").read_text())
        jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
        body = json.dumps({"keys": [{**jwk, "kid": "dast", "alg": "RS256", "use": "sig"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        expected = "Basic " + base64.b64encode(b"dast-machine:ephemeral-test-only").decode()
        body = self.rfile.read(min(int(self.headers.get("Content-Length", "0")), 4096))
        data = parse_qs(body.decode())
        if self.path != "/oauth2/token" or self.headers.get("Authorization") != expected:
            self.send_error(403)
            return
        if data.get("grant_type") != ["client_credentials"]:
            self.send_error(400)
            return
        token = mint(
            "dast-machine", "dast-machine", (), overrides={"scope": "lostlink/members.check"}
        )
        payload = json.dumps(
            {"access_token": token, "expires_in": 900, "token_type": "Bearer"}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("/private/server.pem", "/private/server.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()
