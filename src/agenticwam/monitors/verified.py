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

    def __init__(self, verifier, *, gate=None, watchdog_units=5, batch_verification=False):
        integer(watchdog_units, "watchdog_units", 1, 256)
        if type(batch_verification) is not bool:
            raise ContractError("batch_verification must be boolean")
        self.verifier, self.gate, self.watchdog_units = verifier, gate, watchdog_units
        self.batch_verification = batch_verification
        self._last_check = 0

    def reset(self):
        self._last_check = 0
        if callable(getattr(self.verifier, "reset", None)):
            self.verifier.reset()

    def set_context(self, plan, completed, remaining):
        if callable(getattr(self.verifier, "set_context", None)):
            self.verifier.set_context(plan, completed, remaining)

    @property
    def configuration(self):
        return {
            "watchdog_units": self.watchdog_units,
            "batch_verification": self.batch_verification,
            "required_signal": getattr(self.gate, "name", None),
            "verifier": getattr(self.verifier, "configuration", {}),
        }

    @property
    def last_call_metrics(self):
        return getattr(self.verifier, "last_call_metrics", None)

    def window_size(self, result, confirmations):
        candidate = self.gate is None or self.gate.candidate(result)
        if (
            self.batch_verification
            and candidate
            and result.get("handoff_ready") is True
            and callable(getattr(self.verifier, "verify_many", None))
        ):
            return confirmations
        return 1

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
        return self._gate_verdict(self.verifier.verify(step, before, after, cancel=cancel), result)

    def evaluate_window(self, step, before, observations, result, *, cancel=None):
        values = self.verifier.verify_many(step, before, observations, cancel=cancel)
        if not isinstance(values, list) or len(values) != len(observations):
            raise ContractError("verifier must return one verdict per observation")
        return [self._gate_verdict(value, result) for value in values]

    def _gate_verdict(self, value, result):
        verdict = validate_verdict(value)
        if verdict["verdict"] == "succeeded" and self.gate is not None and not self.gate.candidate(result):
            return {
                "verdict": "unknown",
                "reason": "goal appears complete without the required handoff signal",
                "evidence": verdict["evidence"],
            }
        return verdict
