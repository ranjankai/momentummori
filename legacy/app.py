"""
Momentum Tracker — personal-use web dashboard.

Built on Python's stdlib wsgiref instead of Flask so it runs with zero
extra dependencies beyond pandas/requests (see requirements.txt).

Run with:  python app.py
Then open: http://127.0.0.1:5000

Routes:
  GET  /                 dashboard UI (static/index.html)
  GET  /api/rankings     latest cached ranking result (404 if none yet)
  POST /api/refresh      re-run the pipeline for a given date, cache result
  GET  /api/status       health/log summary
"""

import json
import logging
import logging.handlers
import mimetypes
import os
import traceback
from datetime import date, datetime
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

import config
import nse_client
import pipeline

STATIC_DIR = os.path.join(config.BASE_DIR, "static")


def setup_logging():
    os.makedirs(config.LOG_DIR, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        config.LOG_FILE, maxBytes=2_000_000, backupCount=5
    )
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(formatter)

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    root = logging.getLogger("momentum_tracker")
    root.setLevel(logging.INFO)
    if not root.handlers:
        root.addHandler(handler)
        root.addHandler(console)


setup_logging()
logger = logging.getLogger("momentum_tracker.app")


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """Handle concurrent requests (e.g. a refresh in progress + a status poll)."""

    daemon_threads = True


def _json_bytes(payload) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _respond_json(start_response, status: str, payload: dict):
    body = _json_bytes(payload)
    start_response(
        status,
        [("Content-Type", "application/json"), ("Content-Length", str(len(body)))],
    )
    return [body]


def _serve_static_file(start_response, relative_path: str):
    if relative_path == "" or relative_path == "/":
        relative_path = "index.html"
    # Prevent path traversal outside STATIC_DIR.
    safe_path = os.path.normpath(os.path.join(STATIC_DIR, relative_path.lstrip("/")))
    if not safe_path.startswith(os.path.normpath(STATIC_DIR)):
        return _respond_json(start_response, "403 Forbidden", {"error": "forbidden"})
    if not os.path.isfile(safe_path):
        return _respond_json(start_response, "404 Not Found", {"error": "not found"})

    content_type, _ = mimetypes.guess_type(safe_path)
    with open(safe_path, "rb") as f:
        body = f.read()
    start_response(
        "200 OK",
        [
            ("Content-Type", content_type or "application/octet-stream"),
            ("Content-Length", str(len(body))),
        ],
    )
    return [body]


def _handle_get_rankings(start_response):
    cached = pipeline.load_latest_cached()
    if cached is None:
        return _respond_json(
            start_response,
            "404 Not Found",
            {"error": "No rankings computed yet. Click Refresh to run the pipeline."},
        )
    return _respond_json(start_response, "200 OK", cached)


def _handle_post_refresh(environ, start_response):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    raw_body = environ["wsgi.input"].read(length) if length else b"{}"
    try:
        payload = json.loads(raw_body or b"{}")
    except json.JSONDecodeError:
        return _respond_json(start_response, "400 Bad Request", {"error": "invalid JSON body"})

    date_str = payload.get("as_of")
    try:
        as_of = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else date.today()
    except ValueError:
        return _respond_json(
            start_response, "400 Bad Request",
            {"error": f"Invalid date '{date_str}', expected YYYY-MM-DD"},
        )

    try:
        result = pipeline.run(as_of=as_of)
        return _respond_json(start_response, "200 OK", result)
    except Exception as exc:  # noqa: BLE001 - surface all pipeline failures to the UI
        logger.error("Pipeline run failed: %s\n%s", exc, traceback.format_exc())
        return _respond_json(
            start_response,
            "502 Bad Gateway",
            {
                "error": str(exc),
                "hint": "Check logs/app.log for the full traceback. Common causes: "
                "no NSE trading data for that date, or NSE changed the bhavcopy "
                "file format (see nse_client.py / config.py).",
            },
        )


def _handle_get_status(start_response):
    cached = pipeline.load_latest_cached()
    return _respond_json(
        start_response,
        "200 OK",
        {
            "has_cached_result": cached is not None,
            "last_as_of": cached.get("as_of") if cached else None,
            "last_bhavcopy_date": cached.get("latest_bhavcopy_date") if cached else None,
            "log_file": config.LOG_FILE,
        },
    )


def application(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")
    logger.info("%s %s", method, path)

    try:
        if method == "GET" and path in ("/", ""):
            return _serve_static_file(start_response, "index.html")
        if method == "GET" and path == "/api/rankings":
            return _handle_get_rankings(start_response)
        if method == "POST" and path == "/api/refresh":
            return _handle_post_refresh(environ, start_response)
        if method == "GET" and path == "/api/status":
            return _handle_get_status(start_response)
        if method == "GET" and path.startswith("/static/"):
            return _serve_static_file(start_response, path[len("/static/"):])
        return _respond_json(start_response, "404 Not Found", {"error": f"no route for {method} {path}"})
    except Exception as exc:  # noqa: BLE001 - last-resort guard so the server never crashes
        logger.error("Unhandled error on %s %s: %s\n%s", method, path, exc, traceback.format_exc())
        return _respond_json(start_response, "500 Internal Server Error", {"error": "internal server error"})


def main():
    host, port = "127.0.0.1", 5000
    logger.info("Starting Momentum Tracker on http://%s:%d", host, port)
    with make_server(host, port, application, server_class=ThreadingWSGIServer) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    main()
