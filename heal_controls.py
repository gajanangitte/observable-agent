"""Control plane for the self-healing loop.

The "managed service" is the observable-agent workload. Its runtime config lives
in a small JSON file that both the healer (which writes it) and each canary
rollout (which reads it, via env) share. Actuators change this config; the next
canary rollout reflects the change -- exactly like applying a config and rolling
it out. Keeping the two in separate processes (the canary is a subprocess) means
config.py picks the new values up fresh, with no in-process reload tricks.
"""
import json
import os

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "heal_state.json")

# healthy baseline vs the deliberately-broken starting point (fault injected).
HEALTHY = {"chaos_drop": False, "model": "llama3.2:1b", "mitigation": False}
BROKEN = {"chaos_drop": True, "model": "llama3.2:1b", "mitigation": False}


class Controls:
    """Reads/writes the managed workload's runtime config."""

    def __init__(self, path=STATE_PATH):
        self.path = path
        self.state = self.load()

    def load(self):
        try:
            with open(self.path) as f:
                return {**HEALTHY, **json.load(f)}
        except FileNotFoundError:
            return dict(HEALTHY)

    def save(self):
        with open(self.path, "w") as f:
            json.dump(self.state, f, indent=2)
        return self.state

    def reset(self, broken=True):
        self.state = dict(BROKEN if broken else HEALTHY)
        return self.save()

    def canary_env(self, experiment_id):
        """Env for a canary rollout that reflects the CURRENT managed config.

        A mitigation (idempotency guard) neutralises the injected drop even if
        the fault knob is still armed, so the model has two valid remediations:
        remove the fault at its source, or compensate for it.
        """
        env = dict(os.environ)
        env["OTEL_SERVICE_NAME"] = "observable-agent"      # the managed workload
        env["EXPERIMENT_ID"] = experiment_id
        env["AGENT_MODEL"] = self.state["model"]
        env["AGENT_MAX_OUTPUT_TOKENS"] = "80"   # keep CPU rollouts snappy
        drop = self.state["chaos_drop"] and not self.state["mitigation"]
        env["CHAOS_DROP_RESPONSE_ONCE"] = "1" if drop else "0"
        return env
