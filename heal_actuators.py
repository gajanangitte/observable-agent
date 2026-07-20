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

import heal_policy
import heal_sensors
import economics

# The per-request spend budget the cost circuit breaker arms. Default comes from
# the economics model (economics.yaml budget.per_request_usd, default 0.0001) so
# it is configurable centrally; HEAL_COST_BUDGET_USD still overrides per run.
HEAL_COST_BUDGET_USD = float(
    os.getenv("HEAL_COST_BUDGET_USD", str(economics.default_budget_usd()))
    or economics.default_budget_usd())


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

    def _reject_params(name, why):
        """An action cleared the name gate but its ARGUMENTS were out of bounds.
        Fail closed: touch nothing, stamp the span, and do NOT consume the
        remediation (the model may pick a valid action instead)."""
        sp = trace.get_current_span()
        if sp is not None:
            sp.set_attribute("heal.param.rejected", True)
            sp.set_attribute("heal.param.reason", why)
        return {"action": name, "applied": False, "rejected": True, "reason": why}

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
                           "re-inference that spends the tokens twice; that duplicate "
                           "work is the retry tax."),
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
            "root_cause": ("Each request issues many llm.chat calls that repeat without "
                           "making new progress; the per-request spend scales directly "
                           "with that call count, so the bill grows with the loop."),
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
        ok, why, clean = heal_policy.validate_params("set_cost_budget", {"usd": usd})
        if not ok:
            return _reject_params("set_cost_budget", why)
        budget = clean.get("usd", HEAL_COST_BUDGET_USD)
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
        ok, why, clean = heal_policy.validate_params("switch_model", {"to": to})
        if not ok:
            return _reject_params("switch_model", why)
        target = clean.get("to", "llama3.2:1b")
        controls.state["model"] = target
        controls.save()
        decisions.append(f"switch_model:{target}")
        return {"action": "switch_model", "applied": True, "model": target,
                "effect": f"routed the workload to {target}."}

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
                "description": ("Remove the injected response-drop fault at its source so future "
                                "rollouts stop retrying. This eliminates the root cause, but is "
                                "only possible when you control the fault's source. Takes no "
                                "arguments."),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        "enable_mitigation": {
            "type": "function",
            "function": {
                "name": "enable_mitigation",
                "description": ("Enable an idempotency guard that recovers a completed response "
                                "instead of re-inferring it, neutralising the retry tax. This is "
                                "a compensating control: it stops the waste without removing the "
                                "underlying fault. Takes no arguments."),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        "set_cost_budget": {
            "type": "function",
            "function": {
                "name": "set_cost_budget",
                "description": ("Arm a hard per-request cost budget; a runtime circuit-breaker "
                                "then severs any request whose spend reaches it, structurally "
                                "stopping a runaway loop and capping the bill. A legitimately "
                                "expensive request would also be cut. Takes no arguments."),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        "switch_model": {
            "type": "function",
            "function": {
                "name": "switch_model",
                "description": ("Route the workload to a cheaper or faster model. This lowers "
                                "the cost and latency of EACH call but does not change how MANY "
                                "calls a request makes, so it does not stop a runaway loop."),
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
