"""Named chaos catalog: a deterministic fault library with a CLI.

A self-healing loop is only as trustworthy as the faults you can reproducibly
throw at it. This module names every fault the healer is built to survive, in one
place, so anyone can inject a KNOWN incident deterministically and watch the loop
detect, diagnose, act, verify, and (if needed) roll back. It mirrors the "named
fault catalog" pattern that reliability tooling uses, but every fault here is tied
to (a) the SLO it breaches, (b) the exact control-plane knob it toggles, and (c)
the SigNoz-verified remediation that clears it -- so the catalog doubles as the
map of what the healer can actually close.

Only faults with a REAL injection point are listed (no cosmetic faults): the
response-drop that creates the retry tax, and the runaway llm loop that creates
bill-shock. Each is a whitelisted, reversible config toggle on the shared control
plane (``heal_state.json``) -- exactly what a canary rollout reads -- so injecting
one is a real, observable change, never a monkey-patch.

The pure core (the ``FAULTS`` table, :func:`describe`, :func:`plan`) is stdlib-only
and unit-tested offline. Applying a fault mutates a ``Controls`` instance.

CLI::

    python chaos.py list                 # every named fault
    python chaos.py describe response_drop
    python chaos.py inject response_drop  # arm it on the control plane
    python chaos.py status                # what is armed right now
    python chaos.py clear                 # return the control plane to healthy
"""
import argparse
import sys
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Fault:
    name: str
    slo: str                       # the SLO this fault breaches (heal_sensors)
    summary: str                   # one-line description of the fault mechanism
    control: dict                  # control-plane state that ARMS the fault
    heal_scenario: str             # self_heal.py --scenario that closes it
    remediations: tuple            # verified fixes that clear it (heal_actuators)
    env: dict = field(default_factory=dict)   # equivalent one-shot workload env

    def as_dict(self):
        return {
            "name": self.name, "slo": self.slo, "summary": self.summary,
            "control": dict(self.control), "heal_scenario": self.heal_scenario,
            "remediations": list(self.remediations), "env": dict(self.env),
        }


# The catalog. Keys are stable fault names; every entry maps to a real injection
# point that the canary workload honours (see heal_controls.canary_env / config).
FAULTS = {
    "response_drop": Fault(
        name="response_drop",
        slo="retry_tax",
        summary=("Drop the first COMPLETED llm response of each request once, after "
                 "the tokens are already spent, forcing a re-inference. The duplicate "
                 "work is the retry tax."),
        control={"chaos_drop": True, "mitigation": False},
        heal_scenario="retry",
        remediations=("disable_fault_injection", "enable_mitigation"),
        env={"CHAOS_DROP_RESPONSE_ONCE": "1"}),
    "runaway_loop": Fault(
        name="runaway_loop",
        slo="cost_runaway",
        summary=("Keep issuing llm.chat 'reflection' calls that make no new progress, "
                 "burning tokens in a stuck loop until a per-request cost breaker severs "
                 "it. Left unarmed, it runs the bill up."),
        control={"runaway": True},
        heal_scenario="cost",
        remediations=("set_cost_budget", "switch_model"),
        env={"CHAOS_RUNAWAY": "1"}),
    "carbon_waste": Fault(
        name="carbon_waste",
        slo="carbon_slo",
        summary=("The SAME dropped-and-retried responses, viewed as a GreenOps breach: "
                 "the wasted re-inference burns real joules and grams of CO2e that the "
                 "WattTrace model prices. It shares the response_drop injection point."),
        control={"chaos_drop": True, "mitigation": False},
        heal_scenario="carbon",
        remediations=("disable_fault_injection", "enable_mitigation"),
        env={"CHAOS_DROP_RESPONSE_ONCE": "1"}),
}


def list_faults():
    """Every named fault, in stable order."""
    return list(FAULTS.keys())


def get(name):
    """Return the :class:`Fault` for ``name`` or raise ``KeyError`` with the catalog."""
    if name not in FAULTS:
        raise KeyError(f"unknown fault {name!r}; known faults: {list_faults()}")
    return FAULTS[name]


def describe(name):
    """A JSON-serialisable description of one fault (name, SLO, remediations, ...)."""
    return get(name).as_dict()


def plan(name):
    """The control-plane state that arming ``name`` produces, merged onto healthy.

    A PURE function (no I/O): returns the full control dict the workload would run
    under once this fault is injected, so it can be asserted in tests without
    touching heal_state.json.
    """
    from heal_controls import HEALTHY
    return {**HEALTHY, **get(name).control}


def env_for(name):
    """The one-shot workload env that reproduces ``name`` without the control plane
    (e.g. ``CHAOS_DROP_RESPONSE_ONCE=1``). Pure; mirrors the config knobs."""
    return dict(get(name).env)


def inject(name, controls):
    """Arm ``name`` on a live ``Controls`` instance and persist it. Returns the
    resulting control-plane state. The next canary rollout reflects the fault."""
    fault = get(name)
    controls.state.update(fault.control)
    controls.save()
    return dict(controls.state)


def clear(controls):
    """Disarm every catalogued fault, returning the control plane to healthy."""
    from heal_controls import HEALTHY
    controls.state.update({k: HEALTHY[k] for k in ("chaos_drop", "runaway", "mitigation")})
    controls.save()
    return dict(controls.state)


def armed(controls):
    """Which catalogued faults are currently armed on this control plane."""
    hits = []
    for name, fault in FAULTS.items():
        if all(controls.state.get(k) == v for k, v in fault.control.items()):
            hits.append(name)
    return hits


def _print_fault(name):
    f = get(name)
    print(f"  {f.name}")
    print(f"    slo:          {f.slo}")
    print(f"    heals with:   self_heal.py --scenario {f.heal_scenario}")
    print(f"    remediations: {', '.join(f.remediations)}")
    print(f"    control:      {f.control}")
    print(f"    {f.summary}")


def _main(argv=None):
    ap = argparse.ArgumentParser(description="Named chaos catalog for the self-healer.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list every named fault")
    d = sub.add_parser("describe", help="describe one fault")
    d.add_argument("name")
    i = sub.add_parser("inject", help="arm a fault on the control plane")
    i.add_argument("name")
    sub.add_parser("status", help="show which faults are currently armed")
    sub.add_parser("clear", help="return the control plane to healthy")
    args = ap.parse_args(argv)

    if args.cmd == "list":
        print("named faults (python chaos.py describe <name> for detail):")
        for name in list_faults():
            f = FAULTS[name]
            print(f"  {name:<14} -> breaches {f.slo}, heals via --scenario {f.heal_scenario}")
        return 0
    if args.cmd == "describe":
        _print_fault(args.name)
        return 0

    # inject / status / clear need the live control plane.
    from heal_controls import Controls
    controls = Controls()
    if args.cmd == "inject":
        state = inject(args.name, controls)
        print(f"injected '{args.name}'. control plane is now:")
        print(f"  {state}")
        f = get(args.name)
        print(f"run the heal: python self_heal.py --scenario {f.heal_scenario}")
        return 0
    if args.cmd == "status":
        hits = armed(controls)
        print("armed faults:", ", ".join(hits) if hits else "(none -- healthy)")
        print("control plane:", controls.state)
        return 0
    if args.cmd == "clear":
        state = clear(controls)
        print("cleared all catalogued faults. control plane is now:")
        print(f"  {state}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(_main())
