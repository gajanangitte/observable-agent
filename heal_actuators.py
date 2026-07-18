"""The healer's hands: a read_incident tool plus actuators, exposed to the
decision model as OpenAI tools.

The model first calls ``read_incident`` (which pulls the breach evidence from
SigNoz through the MCP server), then calls exactly one remediation. Each
remediation mutates the shared control plane (persisted to heal_state.json) --
the next canary rollout reflects it. A guard makes the first remediation win, so
a chatty model can't thrash the config.

Every mutation is routed through the **policy gate** first: if the governance
policy does not allow the action (wrong autonomy level, risk above the auto cap,
not pre-approved), the actuator does NOT touch the control plane -- it returns a
"held for approval" result and records the decision on the span. That is what
makes the loop *governed* rather than merely automated.
"""
import os

from opentelemetry import trace

import heal_sensors

HEAL_COST_BUDGET_USD = float(os.getenv("HEAL_COST_BUDGET_USD", "0.0001") or 0.0001)


def build(mcp, controls, cohort, decisions, actions=("disable_fault_injection", "enable_mitigation"),
          policy=None, gate_log=None):
    """Return (schemas, registry) bound to a live MCP client + control plane.

    ``decisions`` is a list the orchestrator inspects to learn what the model
    chose; ``actions`` scopes the remediation options advertised for THIS
    incident (a real self-healing system doesn't offer irrelevant actions).
    ``policy`` (optional) gates every mutation; ``gate_log`` (optional) collects
    the Decision objects so the orchestrator can tell "held for approval" apart
    from "not chosen".
    """

    def _already_remediated():
        return any(d != "read_incident" for d in decisions)

    def _gate(name):
        """Evaluate the policy, stamp the span, log the decision. Returns the
        Decision (or None when ungoverned)."""
        if policy is None:
            return None
        decision = policy.evaluate(name)
        decision.annotate(trace.get_current_span())
        if gate_log is not None:
            gate_log.append(decision)
        return decision

    def _held(decision, name):
        return {"action": name, "applied": False,
                "held_for_approval": decision.requires_approval,
                "policy": decision.reason}

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

    def read_cost_incident(**_):
        slo = heal_sensors.cost_slo(mcp, cohort)
        decisions.append("read_incident")
        return {
            "slo": slo["slo"], "cohort": slo["cohort"],
            "llm_calls": slo["llm_calls"], "requests": slo["requests"],
            "calls_per_request": slo["calls_per_request"],
            "spend_usd": slo["spend_usd"],
            "fault_signature": "runaway llm.chat loop (calls_per_request high)",
            "summary": slo["headline"],
            "root_cause": ("The agent is stuck in a loop, issuing llm.chat calls that do no "
                           "new work and running up the bill. It needs a hard per-request "
                           "spend budget so a runtime circuit-breaker severs the runaway."),
        }

    def disable_fault_injection(**_):
        if _already_remediated():
            return {"applied": False, "note": "already remediated this incident"}
        d = _gate("disable_fault_injection")
        if d is not None and not d.allow:
            return _held(d, "disable_fault_injection")
        controls.state["chaos_drop"] = False
        controls.save()
        decisions.append("disable_fault_injection")
        return {"action": "disable_fault_injection", "applied": True,
                "effect": "removed the injected response-drop at its source; the next "
                          "rollout will not retry."}

    def enable_mitigation(**_):
        if _already_remediated():
            return {"applied": False, "note": "already remediated this incident"}
        d = _gate("enable_mitigation")
        if d is not None and not d.allow:
            return _held(d, "enable_mitigation")
        controls.state["mitigation"] = True
        controls.save()
        decisions.append("enable_mitigation")
        return {"action": "enable_mitigation", "applied": True,
                "effect": "enabled an idempotency guard that recovers the completed "
                          "response instead of re-inferring, neutralising the retry tax."}

    def set_cost_budget(usd: float = None, **_):
        if _already_remediated():
            return {"applied": False, "note": "already remediated this incident"}
        d = _gate("set_cost_budget")
        if d is not None and not d.allow:
            return _held(d, "set_cost_budget")
        budget = float(usd) if usd else HEAL_COST_BUDGET_USD
        controls.state["cost_budget_usd"] = budget
        controls.save()
        decisions.append("set_cost_budget")
        return {"action": "set_cost_budget", "applied": True, "cost_budget_usd": budget,
                "effect": f"armed a ${budget:.6f} per-request cost circuit-breaker; the next "
                          f"rollout structurally severs any request that reaches it, capping "
                          f"the runaway agent's spend."}

    def switch_model(to: str = "llama3.2:1b", **_):
        if _already_remediated():
            return {"applied": False, "note": "already remediated this incident"}
        d = _gate("switch_model")
        if d is not None and not d.allow:
            return _held(d, "switch_model")
        controls.state["model"] = to
        controls.save()
        decisions.append(f"switch_model:{to}")
        return {"action": "switch_model", "applied": True, "model": to,
                "effect": f"routed the workload to {to}."}

    registry = {
        "read_incident": read_cost_incident if "set_cost_budget" in actions else read_incident,
        "disable_fault_injection": disable_fault_injection,
        "enable_mitigation": enable_mitigation,
        "set_cost_budget": set_cost_budget,
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
        "set_cost_budget": {
            "type": "function",
            "function": {
                "name": "set_cost_budget",
                "description": ("Remediate a runaway-spend incident by arming a hard per-request "
                                "cost budget; a runtime circuit-breaker then severs any request "
                                "that reaches it, capping the bill. Takes no arguments."),
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
