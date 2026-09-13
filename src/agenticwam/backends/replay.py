"""Deterministic fixture playback, never a physics or success-rate simulator."""

import json
from copy import deepcopy

from agenticwam.core.types import ContractError


class ReplayBackend:
    def __init__(self, records, initial=None):
        self.records = iter(deepcopy(records))
        self.observation = deepcopy(initial or {"facts": {}})
        self.calls = []
        self.cancelled = []
        self.clock_ns = 1

    def check_capabilities(self):
        return {"backend": "replay", "physical_execution": False, "context_modalities": ["text"]}

    def execute(self, operation_id, context, units, timeout_sec, *, cancel=None):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("replay cancelled")
        record = next(self.records)
        if record["instruction"] != context.text:
            raise ContractError("replay context differs from the recorded action")
        self.calls.append(context)
        self.observation = record["observation"]
        self.clock_ns += 1
        return {
            "state": "completed",
            "finished_monotonic_ns": self.clock_ns,
            "receipt": context.to_dict(),
            "handoff_ready": record.get("handoff_ready", True),
            "completion_signal": record.get("completion_signal", {}),
        }

    def observe(self, directory, *, after_ns=0, cancel=None):
        self.clock_ns = max(self.clock_ns + 1, after_ns + 1)
        return {
            "images": [],
            "metadata": {
                "server_monotonic_ns": self.clock_ns,
                "source": "synthetic_fixture",
                **deepcopy(self.observation),
            },
        }

    def cancel(self, operation_id):
        self.cancelled.append(operation_id)


class PredicateVerifier:
    """Compare declared goal facts against fixture evidence without task names."""

    def preflight(self, step, observation, *, cancel=None):
        facts = observation["metadata"].get("facts", {})
        ready = all(facts.get(condition) is True for condition in step.preconditions)
        return {
            "verdict": "continue" if ready else "blocked",
            "reason": "Declared preconditions checked",
            "evidence": json.dumps(facts),
        }

    def verify(self, step, before, after, *, cancel=None):
        metadata = after["metadata"]
        facts = metadata.get("facts", {})
        goal = json.loads(step.success_criteria)
        if not isinstance(goal, dict) or not goal:
            raise ContractError("predicate verifier requires a nonempty JSON goal mapping")
        if metadata.get("blocked"):
            status = "blocked"
        elif any(key not in facts for key in goal):
            status = "unknown"
        elif all(facts[key] == value for key, value in goal.items()):
            status = "succeeded"
        else:
            status = "continue"
        return {"verdict": status, "reason": "Goal compared with supplied fixture facts", "evidence": json.dumps(facts)}
