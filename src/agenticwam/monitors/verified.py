"""Combine configurable evidence gates with a replaceable goal verifier."""

from agenticwam.core.types import ContractError, integer


class RequiredSignal:
    """Require an adapter-declared event; event semantics belong to the adapter."""

    def __init__(self, name):
        if not isinstance(name, str) or not name:
            raise ContractError("signal name must be nonempty")
        self.name = name

    def candidate(self, result):
        signal = result.get("completion_signal", {})
        if signal.get("type") != self.name or type(signal.get("candidate")) is not bool:
            raise ContractError("backend did not return the required completion signal")
        return signal["candidate"]


def validate_verdict(value):
    if not isinstance(value, dict) or value.get("verdict") not in {"succeeded", "continue", "blocked", "unknown"}:
        raise ContractError("invalid verification verdict")
    for key in ("reason", "evidence"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ContractError("verification requires a reason and observed evidence")
    return value


class VerifiedMonitor:
    """Events schedule checks; only task evidence can establish completion.

    No event name, object category or robot morphology is assumed here.
    Repeated confirmations are a stability heuristic, not calibrated probability.
    """

    def __init__(self, verifier, *, gate=None, watchdog_units=5):
        integer(watchdog_units, "watchdog_units", 1, 256)
        self.verifier, self.gate, self.watchdog_units = verifier, gate, watchdog_units
        self._last_check = 0

    def begin(self, step, observation, *, cancel=None):
        self._last_check = 0
        return validate_verdict(self.verifier.preflight(step, observation, cancel=cancel))

    def should_check(self, result, units):
        candidate = self.gate is None or self.gate.candidate(result)
        if candidate or units - self._last_check >= self.watchdog_units:
            self._last_check = units
            return True
        return False

    def evaluate(self, step, before, after, result, *, cancel=None):
        verdict = validate_verdict(self.verifier.verify(step, before, after, cancel=cancel))
        if verdict["verdict"] == "succeeded" and self.gate is not None and not self.gate.candidate(result):
            return {
                "verdict": "unknown",
                "reason": "goal appears complete without the required handoff signal",
                "evidence": verdict["evidence"],
            }
        return verdict
