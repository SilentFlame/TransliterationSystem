"""Local Xlit Service: runs on the typist's machine, bound to localhost.

Endpoints::

    GET  /                      editor UI (static)
    GET  /v1/suggest?q=mera&k=4&word_start=0   ranked candidates (hot path)
    POST /v1/events             UI telemetry: commit / pick / dismiss (also a word boundary)
    GET  /v1/status             engine + sync state for the UI status bar
    POST /v1/sync               ask the sync agent to check central now
    GET  /healthz

Run::

    python -m xlit.local.server --central-url http://127.0.0.1:8700
"""

from __future__ import annotations

import argparse
import logging
import re
import time
import uuid
from dataclasses import replace
from pathlib import Path

from flask import Flask, Response, g, jsonify, request, send_from_directory

from xlit.common.http import serve
from xlit.common.logging_setup import configure_logging, get_logger, log_event
from xlit.local.central_client import CentralClient
from xlit.local.config import RESCORE_MODES, LocalConfig
from xlit.local.engine_manager import EngineManager, EngineNotReady
from xlit.local.package_store import PackageStore
from xlit.local.sync_agent import SyncAgent

log = get_logger("local.http")
ui_log = get_logger("local.ui")

STATIC_DIR = Path(__file__).parent / "static"
_ROMAN_RE = re.compile(r"^[a-z]+$")
_EVENT_TYPES = {"commit", "dismiss", "error"}
_EVENT_SOURCES = {"space", "enter", "tab", "punct", "digit", "click", "raw", "escape"}


def create_app(cfg: LocalConfig, manager: EngineManager, agent: SyncAgent | None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024

    @app.before_request
    def _start() -> None:
        g.t0 = time.perf_counter()
        g.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]

    @app.after_request
    def _finish(resp: Response) -> Response:
        resp.headers["X-Request-ID"] = g.get("request_id", "-")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        # /v1/suggest and /v1/status log richer, endpoint-specific lines themselves.
        if request.path not in ("/v1/suggest", "/v1/status") or resp.status_code >= 400:
            log_event(log, logging.WARNING if resp.status_code >= 500 else logging.INFO, "http_request",
                      request_id=g.get("request_id"), method=request.method, path=request.path,
                      status=resp.status_code,
                      duration_ms=round((time.perf_counter() - g.get("t0", time.perf_counter())) * 1000, 2))
        return resp

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/static/<path:name>")
    def static_files(name: str):
        return send_from_directory(STATIC_DIR, name)

    @app.get("/healthz")
    def healthz():
        ready = manager.active is not None
        return jsonify(status="ok" if ready else "warming", ready=ready), (200 if ready else 503)

    @app.get("/v1/suggest")
    def suggest():
        q = (request.args.get("q") or "").strip().lower()
        if not q or len(q) > cfg.max_word_len or not _ROMAN_RE.match(q):
            return jsonify(error="bad_request", detail=f"q must be 1-{cfg.max_word_len} latin letters"), 400
        try:
            k = int(request.args.get("k", cfg.default_topk))
        except ValueError:
            return jsonify(error="bad_request", detail="k must be an integer"), 400
        k = max(1, min(k, cfg.beam_width))
        word_start = request.args.get("word_start") in ("1", "true")

        try:
            s = manager.suggest(q, k, word_start=word_start)
        except EngineNotReady:
            return jsonify(error="model_not_ready", detail="waiting for first model package"), 503

        total_ms = round((time.perf_counter() - g.t0) * 1000, 2)
        fields = {
            "request_id": g.request_id,
            "k": k,
            "word_start": word_start,
            "n": len(s.candidates),
            "cached": s.cached,
            "compute_ms": round(s.compute_ms, 2),
            "total_ms": total_ms,
            "version": s.version,
            "tier": s.tier,
            "q_len": len(q),
        }
        if not cfg.redact_content:
            fields["q"] = q
            fields["top1"] = s.candidates[0] if s.candidates else None
        log_event(log, logging.INFO, "suggest", **fields)
        return jsonify(q=q, candidates=s.candidates, version=s.version, tier=s.tier, cached=s.cached,
                       compute_ms=round(s.compute_ms, 2), server_ms=total_ms, request_id=g.request_id)

    @app.post("/v1/events")
    def events():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or body.get("type") not in _EVENT_TYPES:
            return jsonify(error="bad_request"), 400
        source = body.get("source")
        roman = str(body.get("roman", ""))[: cfg.max_word_len]
        chosen = str(body.get("chosen", ""))[:64]
        fields = {
            "type": body["type"],
            "source": source if source in _EVENT_SOURCES else None,
            "rank": body.get("rank") if isinstance(body.get("rank"), int) else None,
            "shown": body.get("shown") if isinstance(body.get("shown"), int) else None,
            "rtt_ms": body.get("rtt_ms") if isinstance(body.get("rtt_ms"), (int, float)) else None,
            "session": str(body.get("session", ""))[:32],
            "roman_len": len(roman),
            "chosen_len": len(chosen),
        }
        if not cfg.redact_content:
            fields["roman"] = roman
            fields["chosen"] = chosen
        log_event(ui_log, logging.INFO, f"ui_{body['type']}", **fields)
        promoted = manager.word_boundary() if body["type"] == "commit" else False
        return jsonify(ok=True, promoted=promoted)

    @app.get("/v1/status")
    def status():
        return jsonify(engine=manager.status(), sync=agent.status() if agent else None,
                       config={"lang": cfg.lang, "beam_width": cfg.beam_width, "topk": cfg.default_topk})

    @app.post("/v1/sync")
    def sync_now():
        if agent:
            agent.poke()
        return jsonify(ok=True)

    return app


def parse_args(argv: list[str] | None = None) -> LocalConfig:
    d = LocalConfig()
    p = argparse.ArgumentParser(description="Local Xlit Service (typist machine)")
    p.add_argument("--central-url", default=d.central_url)
    p.add_argument("--host", default=d.host, help="Keep on 127.0.0.1 unless you know why")
    p.add_argument("--port", type=int, default=d.port)
    p.add_argument("--lang", default=d.lang)
    p.add_argument("--store-dir", type=Path, default=d.store_dir)
    p.add_argument("--log-dir", type=Path, default=d.log_dir)
    p.add_argument("--beam-width", type=int, default=d.beam_width)
    p.add_argument("--topk", type=int, default=d.default_topk)
    p.add_argument("--rescore", choices=RESCORE_MODES, default=d.rescore_mode,
                   help="auto: upgrade to rescore if RAM >= --rescore-min-ram-gb")
    p.add_argument("--rescore-min-ram-gb", type=float, default=d.rescore_min_ram_gb)
    p.add_argument("--poll-interval", type=float, default=d.poll_interval_s, help="Seconds between central checks")
    p.add_argument("--idle-promote", type=float, default=d.idle_promote_s)
    p.add_argument("--cache-size", type=int, default=d.cache_size)
    p.add_argument(
        "--log-content",
        action="store_true",
        help="Log roman prefixes and chosen text (off by default; courtroom text is sensitive)",
    )
    a = p.parse_args(argv)
    return replace(
        d, central_url=a.central_url, host=a.host, port=a.port, lang=a.lang, store_dir=a.store_dir,
        log_dir=a.log_dir, beam_width=a.beam_width, default_topk=min(a.topk, a.beam_width),
        rescore_mode=a.rescore, rescore_min_ram_gb=a.rescore_min_ram_gb, poll_interval_s=a.poll_interval,
        idle_promote_s=a.idle_promote, cache_size=a.cache_size, redact_content=not a.log_content,
    )


def main(argv: list[str] | None = None) -> int:
    started_at = time.time()
    cfg = parse_args(argv)
    log_path = configure_logging("local_client", cfg.log_dir)

    store = PackageStore(cfg.store_dir)
    client = CentralClient(cfg.central_url, client_id=store.client_id())
    manager = EngineManager(cache_size=cfg.cache_size, idle_promote_s=cfg.idle_promote_s)
    agent = SyncAgent(cfg, store, client, manager, started_at=started_at)

    log_event(log, logging.INFO, "local_starting", host=cfg.host, port=cfg.port, central_url=cfg.central_url,
              lang=cfg.lang, beam=cfg.beam_width, topk=cfg.default_topk, rescore_mode=cfg.rescore_mode,
              rescore_allowed=cfg.rescore_allowed(), store=str(cfg.store_dir.resolve()), log_file=str(log_path),
              client_id=client.client_id)
    agent.start()
    print(f"\n  Editor UI: http://{cfg.host}:{cfg.port}/\n", flush=True)
    serve(create_app(cfg, manager, agent), cfg.host, cfg.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
