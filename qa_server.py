"""Dev-only QA server: serves frontend/ statically and real /api/* JSON
computed from the actual project DB via the real api.py business logic
(fastapi/pydantic stubbed since they're not installed in this sandbox — see
api.py's decorated functions being called directly here, bypassing the
FastAPI routing layer entirely, which is why this file exists instead of
just running `uvicorn api:app`)."""
import http.server
import json
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, "/home/claude/fastapi_stub")
sys.path.insert(0, str(Path(__file__).parent / "backend"))
import api  # noqa: E402

FRONTEND_DIR = Path(__file__).parent / "frontend"


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # quiet

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        try:
            if parsed.path == "/api/summary":
                return self._send_json(api.get_summary())
            if parsed.path == "/api/records":
                status = qs.get("status", [None])[0]
                record_type = qs.get("record_type", [None])[0]
                return self._send_json(api.get_records(status=status, record_type=record_type))
            if parsed.path == "/api/baseline":
                return self._send_json(api.get_baseline_comparison())
            if parsed.path.startswith("/api/audit/"):
                record_id = parsed.path.split("/api/audit/")[1]
                return self._send_json(api.get_audit(record_id))
            if parsed.path == "/api/health":
                return self._send_json(api.health())
            if parsed.path == "/api/hero-examples":
                return self._send_json(api.get_hero_examples())
        except api.HTTPException as e:
            return self._send_json({"error": {"status": e.status_code, "message": str(e.detail)}}, e.status_code)
        except Exception as e:
            return self._send_json({"error": {"status": 500, "message": str(e)}}, 500)

        # static file serving — matches the real api.py's StaticFiles mount at /static,
        # plus serving index.html at "/"
        path = parsed.path
        if path == "/":
            path = "/index.html"
        elif path.startswith("/static/"):
            path = path[len("/static"):]
        fpath = FRONTEND_DIR / path.lstrip("/")
        if fpath.exists() and fpath.is_file():
            self.send_response(200)
            ctype = "text/html" if fpath.suffix == ".html" else "text/css" if fpath.suffix == ".css" else "application/javascript" if fpath.suffix == ".js" else "application/octet-stream"
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(fpath.read_bytes())
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8123
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"QA server on http://127.0.0.1:{port}")
    server.serve_forever()
