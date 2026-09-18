from prometheus_client import Counter, Gauge, Histogram

CALLS = Counter("adapter_calls_total", "Adapter outcomes", ["vendor", "capability", "status"])
LATENCY = Histogram("adapter_latency_seconds", "Adapter call duration", ["vendor", "capability"])
DEGRADED = Counter(
    "adapter_degraded_total", "Circuit transitions into degraded mode", ["vendor", "capability"]
)
BUDGET_SPEND = Counter(
    "adapter_spend_inr_total", "Adapter spend charged to the ledger, INR", ["vendor", "capability"]
)
BUDGET_REFUSALS = Counter(
    "adapter_budget_refusals_total",
    "Calls refused by a spend cap before the vendor was called",
    ["vendor", "capability", "scope"],
)
BUDGET_ALERTS = Counter(
    "adapter_budget_alerts_total",
    "Monthly budget thresholds crossed; each fires once per month",
    ["threshold"],
)
BUDGET_DEGRADED = Counter(
    "adapter_budget_degraded_total",
    "Spend-ledger backend failures that fell back to in-process accounting",
)
TASK_BUDGET_STOPS = Counter(
    "task_budget_stops_total",
    "Celery tasks stopped by a spend cap before the vendor was called",
    ["task", "scope"],
)
AUDIT_CHAIN_BREAKS = Gauge(
    "uc3_audit_chain_breaks",
    "Audit chains currently failing verification (PRD E8's nightly re-walk)",
)
AUDIT_CHAIN_LAST_VERIFIED = Gauge(
    "uc3_audit_chain_last_verified_timestamp_seconds",
    "Unix time the chain verification last completed, break or no break",
)
