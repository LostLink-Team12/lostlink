"""Ephemeral JWKS server, accessible only inside the disposable DAST network."""

import json
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jwt.algorithms import RSAAlgorithm


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path != "/.well-known/jwks.json":
            self.send_error(404)
            return
        key = RSAAlgorithm.from_jwk(Path("/private/jwk.json").read_text())
        jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
        body = json.dumps({"keys": [{**jwk, "kid": "dast", "alg": "RS256", "use": "sig"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("/private/server.pem", "/private/server.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()
