"""Lightweight aiohttp server for /health and /metrics."""

from __future__ import annotations

import hmac
import logging
import os

from aiohttp import web

from xiaopaw.observability.metrics import generate_metrics

logger = logging.getLogger(__name__)


def _constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


async def _health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def _metrics(request: web.Request) -> web.Response:
    token = os.environ.get("XIAOPAW_METRICS_TOKEN", "")
    if token:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not _constant_time_equals(
            auth[7:], token
        ):
            return web.json_response({"error": "unauthorized"}, status=401)

    try:
        body = generate_metrics()
        return web.Response(body=body, content_type="text/plain", charset="utf-8")
    except ImportError:
        return web.json_response(
            {"error": "prometheus_client not installed"}, status=501
        )


def create_metrics_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", _health)
    app.router.add_get("/metrics", _metrics)
    return app


async def start_metrics_server(
    host: str = "0.0.0.0", port: int = 8090
) -> web.AppRunner:
    app = create_metrics_app()
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, host, port)
        await site.start()
    except OSError as exc:
        await runner.cleanup()
        raise RuntimeError(
            f"metrics server failed to bind to {host}:{port}: {exc}. "
            "Please set a different port via observability.metrics_port in "
            "config.yaml or ensure no other process is using this port."
        ) from exc
    logger.info("metrics server listening on %s:%d", host, port)
    return runner
