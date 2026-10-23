"""Private, read-only SQL metrics; failed scrapes return 503, never a false zero."""

import os
from http.server import BaseHTTPRequestHandler, HTTPServer

import psycopg

QUERY = """
SELECT
 count(*) FILTER (WHERE state='pending' AND kind='embed' AND available_at<=now()),
 count(*) FILTER (WHERE state='leased' AND kind='embed'),
 count(*) FILTER (WHERE state='pending' AND kind='embed' AND available_at>now()),
 count(*) FILTER (WHERE state='failed' AND kind='embed')
FROM matching.work_jobs
"""
NAMES = ["runnable", "leased", "delayed", "failed"]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        try:
            with psycopg.connect(
                os.environ["PG_CONNECTION"],
                connect_timeout=5,
                options="-c default_transaction_read_only=on -c statement_timeout=5000",
            ) as connection:
                values = connection.execute(QUERY).fetchone()
            body = "".join(
                f"# TYPE lostlink_embedding_{name}_jobs gauge\n"
                f"lostlink_embedding_{name}_jobs {value}\n"
                for name, value in zip(NAMES, values, strict=True)
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(body)
        except psycopg.Error:
            self.send_error(503, "Database metric unavailable")

    def log_message(self, *_args):
        pass


def main():
    HTTPServer(("0.0.0.0", 9108), Handler).serve_forever()


if __name__ == "__main__":
    main()
