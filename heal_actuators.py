"""The healer's hands: a read_incident tool plus actuators, exposed to the
decision model as OpenAI tools.

The model first calls ``read_incident`` (which pulls the breach evidence from
SigNoz through the MCP server), then calls exactly one remediation. Each
remediation mutates the shared control plane (persisted to heal_state.json) --
the next canary rollout reflects it. A guard makes the first remediation win, so
a chatty model can't thrash the config.
"""
import heal_sensors


def build(mcp, controls, cohort, decisions, actions=("disable_fault_injection", "enable_mitigation")):
    """Return (schemas, registry) bound to a live MCP client + control plane.

    ``decisions`` is a list the orchestrator inspects to learn what the model
    chose; ``actions`` scopes the remediation options advertised for THIS
    incident (a real self-healing system doesn't offer irrelevant actions).
    """

    def _already_remediated():
        return any(d != "read_incident" for d in decisions)

    def read_incident(**_):
        slo = heal_sensors.retry_slo(mcp, cohort)
        decisions.append("read_incident")
        return {
            "slo": slo["slo"], "cohort": slo["cohort"],
            "dropped_and_retried": slo["dropped"],
            "total_llm_calls": slo["total_llm_calls"],
            "retry_rate": slo["retry_rate"],
            "fault_signature": "retry.reason=response_dropped",
            "summary": slo["headline"],
            "root_cause": ("An injected fault drops a COMPLETED llm response, forcing a "
                           "re-inference that spends the tokens twice -- the 'retry tax'."),
        }

    def disable_fault_injection(**_):
        if _already_remediated():
            return {"applied": False, "note": "already remediated this incident"}
        controls.state["chaos_drop"] = False
        controls.save()
        decisions.append("disable_fault_injection")
        return {"action": "disable_fault_injection", "applied": True,
                "effect": "removed the injected response-drop at its source; the next "
                          "rollout will not retry."}

    def enable_mitigation(**_):
        if _already_remediated():
            return {"applied": False, "note": "already remediated this incident"}
        controls.state["mitigation"] = True
        controls.save()
        decisions.append("enable_mitigation")
        return {"action": "enable_mitigation", "applied": True,
                "effect": "enabled an idempotency guard that recovers the completed "
                          "response instead of re-inferring, neutralising the retry tax."}

    def switch_model(to: str = "llama3.2:1b", **_):
        if _already_remediated():
            return {"applied": False, "note": "already remediated this incident"}
        controls.state["model"] = to
        controls.save()
        decisions.append(f"switch_model:{to}")
        return {"action": "switch_model", "applied": True, "model": to,
                "effect": f"routed the workload to {to}."}

    registry = {
        "read_incident": read_incident,
        "disable_fault_injection": disable_fault_injection,
        "enable_mitigation": enable_mitigation,
        "switch_model": switch_model,
    }

    catalogue = {
        "read_incident": {
            "type": "function",
            "function": {
                "name": "read_incident",
                "description": ("Pull the current incident's evidence from SigNoz: how many of "
                                "this rollout's LLM calls were dropped and retried, the retry "
                                "rate, and the root cause. Call this FIRST. Takes no arguments."),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        "disable_fault_injection": {
            "type": "function",
            "function": {
                "name": "disable_fault_injection",
                "description": ("Remediate by removing the injected response-drop fault at its "
                                "source, so future rollouts stop retrying. Takes no arguments."),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        "enable_mitigation": {
            "type": "function",
            "function": {
                "name": "enable_mitigation",
                "description": ("Remediate by enabling an idempotency guard that recovers a "
                                "completed response instead of re-inferring it, neutralising the "
                                "retry tax. Takes no arguments."),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        "switch_model": {
            "type": "function",
            "function": {
                "name": "switch_model",
                "description": "Route the workload to a faster/cheaper model to cut latency.",
                "parameters": {
                    "type": "object",
                    "properties": {"to": {"type": "string", "description": "e.g. llama3.2:1b"}},
                },
            },
        },
    }
    advertised = ["read_incident"] + [a for a in actions]
    schemas = [catalogue[name] for name in advertised]
    return schemas, registry
