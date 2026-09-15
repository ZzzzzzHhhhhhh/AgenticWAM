"""Structural extension points; implementations are assembled outside the core."""

from pathlib import Path
from threading import Event
from typing import Protocol

from agenticwam.core.types import Context, Plan, Request, Step


class StructuredModel(Protocol):
    model: str

    def ask(self, prompt: str, schema: dict, *, images=(), cancel: Event | None = None) -> dict: ...


class Planner(Protocol):
    def plan(self, request: Request, *, cancel: Event | None = None) -> Plan: ...


class ContextCompiler(Protocol):
    def compile(self, step: Step, context_id: str, revision: int) -> Context: ...


class Backend(Protocol):
    def check_capabilities(self): ...

    def observe(self, directory: Path, *, after_ns: int = 0, cancel: Event | None = None) -> dict: ...

    def execute(
        self, operation_id: str, context: Context, units: int, timeout_sec: float, *, cancel: Event | None = None
    ) -> dict: ...

    def cancel(self, operation_id: str): ...


class Monitor(Protocol):
    def begin(self, step: Step, observation: dict, *, cancel: Event | None = None) -> dict: ...

    def should_check(self, result: dict, units: int) -> bool: ...

    def evaluate(self, step: Step, before: dict, after: dict, result: dict, *, cancel: Event | None = None) -> dict: ...


class WindowMonitor(Monitor, Protocol):
    """Optional batching extension; old Monitor plugins use sequential checks."""

    def window_size(self, result: dict, confirmations: int) -> int: ...

    def evaluate_window(
        self, step: Step, before: dict, observations: list[dict], result: dict, *, cancel: Event | None = None
    ) -> list[dict]: ...


class Replanner(Protocol):
    def revise(
        self, plan: Plan, completed: tuple[Step, ...], failed: Step, evidence: dict, *, cancel: Event | None = None
    ) -> Plan: ...
