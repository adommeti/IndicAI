from prometheus_client import Counter, Histogram

CALLS = Counter("adapter_calls_total", "Adapter outcomes", ["vendor", "capability", "status"])
LATENCY = Histogram("adapter_latency_seconds", "Adapter call duration", ["vendor", "capability"])
DEGRADED = Counter(
    "adapter_degraded_total", "Circuit transitions into degraded mode", ["vendor", "capability"]
)
