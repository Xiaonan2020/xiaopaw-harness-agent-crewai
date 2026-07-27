"""Prometheus metrics definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prometheus_client import CollectorRegistry

try:
    from prometheus_client import (
        CollectorRegistry as _CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    REGISTRY: CollectorRegistry | None = _CollectorRegistry()

    inbound_total = Counter(
        "xiaopaw_inbound_total",
        "Total inbound messages",
        ["source", "routing_type", "has_attachment"],
        registry=REGISTRY,
    )
    llm_calls_total = Counter(
        "xiaopaw_llm_calls_total",
        "Total LLM API calls",
        ["model", "status"],
        registry=REGISTRY,
    )
    agent_latency = Histogram(
        "xiaopaw_agent_latency_seconds",
        "Agent processing latency",
        ["routing_type"],
        registry=REGISTRY,
        buckets=(1, 5, 10, 30, 60, 120, 300),
    )
    llm_latency = Histogram(
        "xiaopaw_llm_latency_seconds",
        "LLM API latency",
        ["model"],
        registry=REGISTRY,
        buckets=(0.5, 1, 2, 5, 10, 30, 60),
    )
    external_api_retry_total = Counter(
        "xiaopaw_external_api_retry_total",
        "External API retries",
        ["api"],
        registry=REGISTRY,
    )
    skill_timeout_total = Counter(
        "xiaopaw_skill_timeout_total",
        "Skill execution timeouts",
        ["skill"],
        registry=REGISTRY,
    )
    feishu_rate_limit_total = Counter(
        "xiaopaw_feishu_rate_limit_total",
        "Feishu API rate limit hits",
        registry=REGISTRY,
    )
    cron_dlq_total = Counter(
        "xiaopaw_cron_dlq_total",
        "Cron jobs sent to dead-letter queue",
        registry=REGISTRY,
    )
    runner_workers_active = Gauge(
        "xiaopaw_runner_workers_active",
        "Number of active per-routing_key workers in Runner",
        ["routing_key_type"],
        registry=REGISTRY,
    )
    runner_queue_size = Gauge(
        "xiaopaw_runner_queue_size",
        "Queue size per routing_key in Runner",
        ["routing_key_type"],
        registry=REGISTRY,
    )
    errors_total = Counter(
        "xiaopaw_errors_total",
        "Errors encountered by various components",
        ["component", "error_type"],
        registry=REGISTRY,
    )

    def generate_metrics() -> bytes:
        """Return the latest Prometheus metrics payload."""
        return generate_latest(REGISTRY)

except ImportError:
    # Stub metrics when prometheus_client is not installed so the rest of the
    # application can still import and call these helpers without crashing.
    REGISTRY = None

    class _Stub:
        def labels(self, *a, **kw):
            return self

        def inc(self, *a, **kw):  # noqa: D401
            pass

        def observe(self, *a, **kw):  # noqa: D401
            pass

        def set(self, *a, **kw):  # noqa: D401
            pass

    inbound_total = _Stub()
    llm_calls_total = _Stub()
    agent_latency = _Stub()
    llm_latency = _Stub()
    external_api_retry_total = _Stub()
    skill_timeout_total = _Stub()
    feishu_rate_limit_total = _Stub()
    cron_dlq_total = _Stub()
    runner_workers_active = _Stub()
    runner_queue_size = _Stub()
    errors_total = _Stub()

    def generate_metrics() -> bytes:  # noqa: D103
        raise ImportError("prometheus_client is not installed")


def routing_key_type(routing_key: str) -> str:
    """Derive a stable routing_type label from a routing_key."""
    if routing_key.startswith("p2p:"):
        return "p2p"
    if routing_key.startswith("group:"):
        return "group"
    if routing_key.startswith("thread:"):
        return "thread"
    return "unknown"


def record_inbound_message(source: str, routing_key: str,has_attachment: bool) -> None:
    """Record an inbound message."""
    inbound_total.labels(
        source=source or "unknown",
        routing_type=routing_key_type(routing_key),
        has_attachment="true" if has_attachment else "false",
    ).inc()


def record_cron_dlq() -> None:
    """Record a cron job sent to the dead-letter queue."""
    cron_dlq_total.inc()


def record_llm_call(model: str, status: str) -> None:
    """Record an LLM API call with its final status."""
    llm_calls_total.labels(
        model=model or "unknown",
        status=status or "unknown",
    ).inc()


def observe_llm_latency(model: str, elapsed: float) -> None:
    """Record LLM API latency in seconds."""
    llm_latency.labels(model=model or "unknown").observe(elapsed)


def observe_agent_latency(routing_key: str, elapsed: float) -> None:
    """Record agent processing latency in seconds."""
    agent_latency.labels(routing_type=routing_key_type(routing_key)).observe(elapsed)


def record_external_api_retry(api: str) -> None:
    """Record an external API retry."""
    external_api_retry_total.labels(api=api or "unknown").inc()


def record_skill_timeout(skill: str) -> None:
    """Record a skill execution timeout."""
    skill_timeout_total.labels(skill=skill or "unknown").inc()


def record_feishu_rate_limit() -> None:
    """Record a Feishu API rate-limit hit."""
    feishu_rate_limit_total.inc()





def record_error(component: str, error_type: str) -> None:
    """Record a generic error from a component."""
    errors_total.labels(
        component=component or "unknown",
        error_type=error_type or "unknown",
    ).inc()


def set_runner_workers(routing_key_type: str, count: int) -> None:
    """Set the number of active workers for a routing_key_type."""
    runner_workers_active.labels(routing_key_type=routing_key_type).set(count)


def set_runner_queue_size(routing_key_type: str, size: int) -> None:
    """Set the queue size for a routing_key_type."""
    runner_queue_size.labels(routing_key_type=routing_key_type).set(size)
