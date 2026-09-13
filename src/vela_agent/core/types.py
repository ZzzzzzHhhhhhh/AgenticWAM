"""Validated, JSON-serializable contracts without robot or model dependencies."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass


class ContractError(ValueError):
    """An invalid value crossed a component boundary."""


class NeedsClarification(ContractError):
    """A mission needs additional human input before execution."""


def text(value, name, limit=4096):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ContractError(f"{name} must contain 1..{limit} characters")
    return value


def identifier(value, name):
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,60}", value) is None:
        raise ContractError(f"{name} must be a short alphanumeric identifier")


def integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ContractError(f"{name} must be an integer in {low}..{high}")


@dataclass(frozen=True)
class MediaRef:
    """Reference only; input adapters decide how to resolve and encode media."""

    kind: str
    uri: str

    def __post_init__(self):
        if self.kind not in {"image", "video"}:
            raise ContractError("unsupported media kind")
        text(self.uri, "media URI")


@dataclass(frozen=True)
class Request:
    request_id: str
    text: str
    media: tuple[MediaRef, ...] = ()

    def __post_init__(self):
        text(self.request_id, "request_id")
        text(self.text, "mission", 32768)
        object.__setattr__(self, "media", tuple(self.media))
        if len(self.media) > 64 or any(not isinstance(item, MediaRef) for item in self.media):
            raise ContractError("invalid media references")


@dataclass(frozen=True)
class Step:
    step_id: str
    instruction: str
    success_criteria: str
    rationale: str
    max_duration_ns: int
    skill: str = "language"
    preconditions: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()

    def __post_init__(self):
        identifier(self.step_id, "step_id")
        for field in ("instruction", "success_criteria", "rationale", "skill"):
            text(getattr(self, field), field)
        integer(self.max_duration_ns, "max_duration_ns", 1, 600_000_000_000)
        for field in ("preconditions", "constraints"):
            values = getattr(self, field)
            if isinstance(values, str):
                raise ContractError(f"{field} must be a sequence")
            values = tuple(values)
            if len(values) > 64:
                raise ContractError(f"too many {field}")
            for value in values:
                text(value, field)
            object.__setattr__(self, field, values)

    @property
    def args(self):
        """Compatibility view for existing step consumers."""
        return {"instruction": self.instruction, "success_criteria": self.success_criteria}


@dataclass(frozen=True)
class Plan:
    plan_id: str
    request_id: str
    steps: tuple[Step, ...]
    summary: str
    planner: str
    mission: str = ""

    def __post_init__(self):
        identifier(self.plan_id, "plan_id")
        for field in ("request_id", "summary", "planner"):
            text(getattr(self, field), field)
        if not isinstance(self.mission, str) or len(self.mission) > 32768:
            raise ContractError("invalid original mission")
        object.__setattr__(self, "steps", tuple(self.steps))
        if not 1 <= len(self.steps) <= 64 or any(not isinstance(s, Step) for s in self.steps):
            raise ContractError("plan requires 1..64 validated steps")
        if len({s.step_id for s in self.steps}) != len(self.steps):
            raise ContractError("duplicate step identifiers")

    def to_dict(self):
        return json.loads(json.dumps({"schema": "vela.agent.plan/v1", **asdict(self)}))

    @classmethod
    def from_dict(cls, value):
        if (
            not isinstance(value, dict)
            or set(value) != {"schema", "plan_id", "request_id", "steps", "summary", "planner", "mission"}
            or value["schema"] != "vela.agent.plan/v1"
        ):
            raise ContractError("invalid plan envelope")
        if not isinstance(value["steps"], list):
            raise ContractError("steps must be an array")
        try:
            return cls(
                **{k: v for k, v in value.items() if k not in {"schema", "steps"}},
                steps=tuple(Step(**row) for row in value["steps"]),
            )
        except TypeError as exc:
            raise ContractError("invalid step fields") from exc


@dataclass(frozen=True)
class Context:
    context_id: str
    revision: int
    text: str

    def __post_init__(self):
        text(self.context_id, "context_id", 128)
        integer(self.revision, "revision", 1, 2**53 - 1)
        text(self.text, "context text")

    def to_dict(self):
        return {"schema": "vela.agent.context/v1", **asdict(self)}

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class RunSettings:
    units_per_check: int = 1
    max_units_per_step: int = 40
    confirmations: int = 2
    confirmation_interval_ns: int = 300_000_000
    max_replans: int = 0

    def __post_init__(self):
        integer(self.units_per_check, "units_per_check", 1, 32)
        integer(self.max_units_per_step, "max_units_per_step", self.units_per_check, 256)
        integer(self.confirmations, "confirmations", 1, 8)
        integer(self.confirmation_interval_ns, "confirmation_interval_ns", 1, 10_000_000_000)
        integer(self.max_replans, "max_replans", 0, 8)
