"""Tools the SRE agent can call.

Deterministic fake data keeps traces reproducible and gives us both healthy
and failing paths to observe (e.g. an unknown service returns an error).
"""

_SERVICES = {
    "checkout":  {"latency_p95_ms": 820, "error_rate": 0.087, "status": "degraded"},
    "payments":  {"latency_p95_ms": 140, "error_rate": 0.002, "status": "healthy"},
    "search":    {"latency_p95_ms": 95,  "error_rate": 0.001, "status": "healthy"},
    "inventory": {"latency_p95_ms": 610, "error_rate": 0.041, "status": "degraded"},
}

_RUNBOOKS = {
    "latency": "Runbook L-2: check downstream DB pool saturation; scale replicas; "
               "inspect slow queries in APM traces; roll back the last deploy if p95 regressed.",
    "error":   "Runbook E-1: pull recent exceptions; check error-budget burn; "
               "enable the circuit breaker; page on-call if error_rate > 5%.",
    "deploy":  "Runbook D-3: verify canary metrics; compare pre/post-deploy latency; "
               "use `foundryctl rollback` to revert.",
}

_DEPLOYS = {
    "checkout": ["v2.4.1 (32m ago)", "v2.4.0 (6h ago)"],
    "payments": ["v1.9.3 (2d ago)"],
    "search": ["v3.1.0 (1d ago)"],
    "inventory": ["v0.8.7 (11m ago)", "v0.8.6 (3h ago)"],
}


def _normalize_service(service: str) -> str:
    """Small local models sometimes pass 'checkout service' or 'the checkout'.
    Normalize to a known key by stripping filler words and matching substrings."""
    s = str(service).lower().strip()
    for junk in ("the ", " service", " svc", "-service"):
        s = s.replace(junk, "").strip()
    if s in _SERVICES:
        return s
    for key in _SERVICES:
        if key in s or s in key:
            return key
    return s


def get_service_health(service: str) -> dict:
    key = _normalize_service(service)
    svc = _SERVICES.get(key)
    if not svc:
        return {"error": f"unknown service '{service}'", "known_services": list(_SERVICES)}
    return {"service": key, **svc}


def search_runbook(topic: str) -> dict:
    t = str(topic).lower()
    for key, text in _RUNBOOKS.items():
        if key in t:
            return {"topic": key, "runbook": text}
    return {"topic": topic, "runbook": "No specific runbook found; escalate to #sre."}


def list_recent_deploys(service: str) -> dict:
    key = _normalize_service(service)
    return {"service": key, "deploys": _DEPLOYS.get(key, [])}


def calculate_error_budget(slo_percent: float, actual_error_rate: float) -> dict:
    allowed = 1 - (float(slo_percent) / 100.0)
    burn = (float(actual_error_rate) / allowed) if allowed > 0 else float("inf")
    return {
        "slo_percent": slo_percent,
        "allowed_error_rate": round(allowed, 5),
        "actual_error_rate": actual_error_rate,
        "budget_burn_ratio": round(burn, 2),
        "over_budget": burn > 1,
    }


REGISTRY = {
    "get_service_health": get_service_health,
    "search_runbook": search_runbook,
    "list_recent_deploys": list_recent_deploys,
    "calculate_error_budget": calculate_error_budget,
}

# OpenAI-style tool schemas advertised to the model.
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_service_health",
            "description": "Get current p95 latency, error rate, and status for a service.",
            "parameters": {
                "type": "object",
                "properties": {"service": {"type": "string", "description": "e.g. checkout"}},
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_runbook",
            "description": "Look up the SRE runbook for a topic such as latency, error, or deploy.",
            "parameters": {
                "type": "object",
                "properties": {"topic": {"type": "string"}},
                "required": ["topic"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_recent_deploys",
            "description": "List recent deploys for a service (most recent first).",
            "parameters": {
                "type": "object",
                "properties": {"service": {"type": "string"}},
                "required": ["service"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_error_budget",
            "description": "Compute error-budget burn from an SLO percent and an actual error rate.",
            "parameters": {
                "type": "object",
                "properties": {
                    "slo_percent": {"type": "number", "description": "e.g. 99.9"},
                    "actual_error_rate": {"type": "number", "description": "e.g. 0.087"},
                },
                "required": ["slo_percent", "actual_error_rate"],
            },
        },
    },
]
