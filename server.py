"""HTTP front for the calculation core. JSON in, JSON out, shared-secret key on every request.

    EPHEMERIS_API_KEY=... python3 server.py [--host 127.0.0.1] [--port 8765]

Standard library only, apart from pyswisseph. Binds to localhost by default. Request bodies
carry birth data, so they are never logged.

Licensed AGPL-3.0, see LICENSE.
"""
import argparse
import hmac
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # works under python -I too
import core  # noqa: E402

MAX_BODY = 256 * 1024
MIN_KEY_LENGTH = 16
ROUTES = {
    "/chart": core.chart,
    "/new-moons": core.new_moons,
    "/transits": core.transits,
    "/house-of": core.house_of,
    "/progressions": core.progressions,
}
# pyswisseph keeps global state in the C library, so calculations take turns.
_calc_lock = threading.Lock()


def make_server(host, port, api_key):
    if not isinstance(api_key, str) or len(api_key) < MIN_KEY_LENGTH:
        raise SystemExit(f"EPHEMERIS_API_KEY must be set and at least {MIN_KEY_LENGTH} characters")
    key = api_key.encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "ephemeris-service"
        sys_version = ""
        timeout = 15

        def log_message(self, fmt, *args):  # method, path and status only; never the body
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        def _send(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self):
            given = self.headers.get("X-API-Key")
            if given is None:
                auth = self.headers.get("Authorization", "")
                if auth.lower().startswith("bearer "):
                    given = auth[7:].strip()
            return given is not None and hmac.compare_digest(given.encode(), key)

        def _handle(self):
            # Authentication first, for every method and path, before anything else is read.
            if not self._authorized():
                return self._send(401, {"error": "missing or invalid API key"})
            if self.command != "POST":
                return self._send(405, {"error": "use POST"})
            route = ROUTES.get(self.path.split("?")[0])
            if route is None:
                return self._send(404, {"error": "unknown endpoint", "endpoints": list(ROUTES)})
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._send(411, {"error": "Content-Length required"})
            if length < 0 or length > MAX_BODY:
                return self._send(413, {"error": f"body limit is {MAX_BODY} bytes"})
            try:
                body = json.loads(self.rfile.read(length) or b"null")
            except (ValueError, UnicodeDecodeError):
                return self._send(400, {"error": "body is not valid JSON"})
            try:
                with _calc_lock:
                    result = route(body)
            except core.InputError as e:
                return self._send(400, {"error": str(e)})
            except Exception:  # never echo internals; the caller can retry or report
                self.log_message("internal error on %s", self.path)
                return self._send(500, {"error": "calculation failed"})
            self._send(200, result)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle

    return ThreadingHTTPServer((host, port), Handler)


def main():
    ap = argparse.ArgumentParser(description="Calculation-only ephemeris service (AGPL-3.0)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        sys.stderr.write("warning: binding beyond localhost; put TLS in front, the API key travels in a header\n")
    srv = make_server(args.host, args.port, os.environ.get("EPHEMERIS_API_KEY"))
    sys.stderr.write(f"listening on http://{args.host}:{srv.server_address[1]}\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
