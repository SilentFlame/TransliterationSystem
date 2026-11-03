"""Central Model Registry HTTP service.

This is **not** on the per-keystroke path. It answers three questions for the
fleet of local services:

* "What is the latest model for <lang>?"      GET  /v1/models/<lang>/latest
* "Give me file X of version V."               GET  /v1/models/<lang>/<version>/files/<path>
* "Here is my health / version."               POST /v1/heartbeat

Run::

    python -m xlit.central.server --port 8700
"""

from __future__ import annotations

import argparse
import logging
import random
import time
import uuid
from pathlib import Path

from flask import Flask, Response, g, jsonify, request, send_file

from xlit.common.http import serve
from xlit.common.logging_setup import configure_logging, get_logger, log_event
from xlit.common.manifest import ManifestError
from xlit.central.registry import ModelRegistry

log = get_logger("central.http")
fleet_log = get_logger("central.fleet")

MAX_HEARTBEAT_BYTES = 4096


def create_app(registry: ModelRegistry, chaos_fail_rate: float = 0.0, chaos_delay_ms: int = 0) -> Flask:
    """``chaos_*`` knobs simulate a flaky courtroom uplink for demos and tests."""
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_HEARTBEAT_BYTES

    @app.before_request
    def _start() -> Response | None:
        g.t0 = time.perf_counter()
        g.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        if request.path.startswith("/v1/") and chaos_delay_ms:
            time.sleep(chaos_delay_ms / 1000.0)
        if request.path.startswith("/v1/models") and chaos_fail_rate and random.random() < chaos_fail_rate:
            return jsonify(error="chaos_injected_failure"), 503
        return None

    @app.after_request
    def _finish(resp: Response) -> Response:
        resp.headers["X-Request-ID"] = g.get("request_id", "-")
        level = logging.WARNING if resp.status_code >= 500 else logging.INFO
        log_event(
            log,
            level,
            "http_request",
            request_id=g.get("request_id"),
            method=request.method,
            path=request.path,
            status=resp.status_code,
            range=request.headers.get("Range"),
            client_id=request.headers.get("X-Client-ID"),
            remote=request.remote_addr,
            duration_ms=round((time.perf_counter() - g.get("t0", time.perf_counter())) * 1000, 2),
        )
        return resp

    @app.errorhandler(ManifestError)
    def _bad_input(e: ManifestError):
        return jsonify(error="bad_request", detail=str(e)), 400

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok")

    @app.get("/v1/models/<lang>/latest")
    def latest(lang: str):
        version = registry.latest_version(lang)
        if version is None:
            return jsonify(error="no_published_version", lang=lang), 404
        etag = f'"{lang}:{version}"'
        if request.headers.get("If-None-Match") == etag:
            resp = Response(status=304)
        else:
            resp = jsonify(registry.get_manifest(lang, version).to_dict())
        resp.headers["ETag"] = etag
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/v1/models/<lang>")
    def versions(lang: str):
        return jsonify(lang=lang, latest=registry.latest_version(lang), versions=registry.list_versions(lang))

    @app.get("/v1/models/<lang>/<version>/manifest")
    def manifest(lang: str, version: str):
        m = registry.get_manifest(lang, version)
        if m is None:
            return jsonify(error="unknown_version"), 404
        resp = jsonify(m.to_dict())
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp

    @app.get("/v1/models/<lang>/<version>/files/<path:relpath>")
    def artifact(lang: str, version: str, relpath: str):
        try:
            path, entry = registry.artifact_path(lang, version, relpath)
        except FileNotFoundError:
            return jsonify(error="not_found"), 404
        # conditional=True gives us Range / 206 support so clients can resume.
        resp = send_file(path, mimetype="application/octet-stream", conditional=True, etag=entry.sha256)
        resp.headers["X-Content-SHA256"] = entry.sha256
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp

    @app.post("/v1/heartbeat")
    def heartbeat():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="bad_request"), 400
        allowed = {
            "client_id", "lang", "active_version", "active_tier", "staged", "central_state",
            "rescore_mode", "download", "uptime_s", "lookups", "cache_hit_rate", "p95_compute_ms",
        }
        log_event(fleet_log, logging.INFO, "client_heartbeat", **{k: body.get(k) for k in allowed if k in body})
        latest_version = registry.latest_version(body.get("lang", "hi")) if body.get("lang") else None
        return jsonify(ok=True, latest_version=latest_version)

    return app


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Central Model Registry server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8700)
    p.add_argument("--registry", type=Path, default=Path("var/registry"))
    p.add_argument("--log-dir", type=Path, default=Path("logs"))
    p.add_argument("--chaos-fail-rate", type=float, default=0.0, help="Fraction of /v1/models requests to 503")
    p.add_argument("--chaos-delay-ms", type=int, default=0, help="Added latency on /v1 requests")
    args = p.parse_args(argv)

    log_path = configure_logging("central_server", args.log_dir)
    registry = ModelRegistry(args.registry)
    log_event(
        log,
        logging.INFO,
        "central_starting",
        host=args.host,
        port=args.port,
        registry=str(args.registry.resolve()),
        latest_hi=registry.latest_version("hi"),
        log_file=str(log_path),
        chaos_fail_rate=args.chaos_fail_rate,
        chaos_delay_ms=args.chaos_delay_ms,
    )
    serve(create_app(registry, args.chaos_fail_rate, args.chaos_delay_ms), args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
