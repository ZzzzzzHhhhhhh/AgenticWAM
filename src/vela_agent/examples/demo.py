"""Small, reproducible fixtures for three task families using one runner."""

import json

from vela_agent.backends.replay import PredicateVerifier, ReplayBackend
from vela_agent.contexts.text import TextCompiler
from vela_agent.core.runner import Runner
from vela_agent.core.types import Plan, Step
from vela_agent.monitors.verified import VerifiedMonitor


def make_demo(scenario="mixed"):
    tasks = {
        "plates": [
            (f"Place the {color} plate in the next rack slot.", f"{color}_placed")
            for color in ("red", "yellow", "blue", "green")
        ],
        "mixed": [
            ("Place the cup on the tray.", "cup_on_tray"),
            ("Push the block into the marked region.", "block_in_region"),
            ("Press the button until the indicator lights up.", "indicator_on"),
        ],
    }
    rows = tasks[scenario]
    facts, records, steps = {}, [], []
    for index, (instruction, key) in enumerate(rows):
        steps.append(
            Step(
                f"step-{index + 1}",
                instruction,
                json.dumps({key: True}),
                "Follow the declared sequence",
                60_000_000_000,
            )
        )
        facts[key] = True
        records.append({"instruction": instruction, "observation": {"facts": dict(facts)}})
    return Plan("demo-" + scenario, "demo", tuple(steps), "Deterministic interface demonstration", "fixture"), records


def run_demo(output, scenario="mixed", on_event=None):
    plan, records = make_demo(scenario)
    runner = Runner(
        ReplayBackend(records), VerifiedMonitor(PredicateVerifier()), TextCompiler(), output, on_event=on_event
    )
    return runner.run(plan)
