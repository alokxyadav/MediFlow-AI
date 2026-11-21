"""
Minimal Prometheus-compatible metrics, hand-rolled with the small
`prometheus_client` library rather than a heavier instrumentation
framework - just request counts and latency, which covers the two
questions you actually ask first when something's wrong in production:
"is it slower than usual?" and "is it erroring more than usual?"

Exposed at GET /metrics (see main.py) - scrape with Prometheus, or point
Grafana Cloud / Datadog / any OpenMetrics-compatible collector at it.
Disable entirely with METRICS_ENABLED=false if you don't use Prometheus.
"""
import time

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

REQUEST_COUNT = Counter(
    "mediflow_http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)

REQUEST_LATENCY = Histogram(
    "mediflow_http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)


def _route_template(request) -> str:
    """Use the matched route's path template (e.g. /api/appointments/{id})
    rather than the raw URL, so metrics aggregate correctly instead of
    creating a new time series per appointment ID."""
    route = request.scope.get("route")
    if route is not None and getattr(route, "path", None):
        return route.path
    return request.url.path


async def metrics_middleware(request, call_next):
    if request.url.path == "/metrics":
        # Don't measure scraping itself.
        return await call_next(request)

    start = time.perf_counter()
    response = await call_next(request)
    duration = time.perf_counter() - start

    path = _route_template(request)
    REQUEST_COUNT.labels(
        method=request.method, path=path, status=response.status_code
    ).inc()
    REQUEST_LATENCY.labels(method=request.method, path=path).observe(duration)

    return response


def render_metrics() -> str:
    return generate_latest().decode("utf-8")


METRICS_CONTENT_TYPE = CONTENT_TYPE_LATEST
