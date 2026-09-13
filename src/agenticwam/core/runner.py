"""Bounded orchestration with injected policy, compiler, monitor and replanner."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from contextlib import suppress
from copy import deepcopy
from pathlib import Path

from agenticwam.core.types import ContractError, Plan, RunSettings


class TaskBlocked(RuntimeError):
    def __init__(self, reason, evidence):
        super().__init__(reason)
        self.evidence = evidence


class Runner:
    def __init__(self, backend, monitor, compiler, output_dir, *, settings=None, replanner=None, on_event=None):
        self.backend, self.monitor, self.compiler = backend, monitor, compiler
        self.settings = settings or RunSettings()
        self.replanner = replanner
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.on_event = on_event or (lambda _event: None)
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._active = False
        self._state = {"status": "idle"}
        self._events = deque(maxlen=2048)
        self._event_sequence = 0
        self._active_operation = None

    def snapshot(self):
        with self._lock:
            return deepcopy(self._state)

    def cancel(self):
        self.cancel_event.set()

    def events(self, after_sequence=0, limit=100):
        if type(after_sequence) is not int or after_sequence < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid event cursor or limit")
        with self._lock:
            oldest = self._events[0]["sequence"] if self._events else 1
            rows = [e for e in self._events if e["sequence"] > after_sequence][:limit]
            return {
                "events": deepcopy(rows),
                "oldest_sequence": oldest,
                "next_sequence": rows[-1]["sequence"] if rows else after_sequence,
                "truncated": after_sequence < oldest - 1,
            }

    def _event(self, run_dir, kind, **fields):
        with self._lock:
            self._event_sequence += 1
            event = {
                "schema": "agenticwam.event/v1",
                "sequence": self._event_sequence,
                "plan_id": self._state.get("plan_id"),
                "event": kind,
                "wall_ns": time.time_ns(),
                **fields,
            }
        with (run_dir / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        with self._lock:
            self._events.append(deepcopy(event))
        # UI failures must not change the physical task's lifecycle.
        with suppress(Exception):
            self.on_event(deepcopy(event))

    def _check(self, deadline=None):
        if self.cancel_event.is_set():
            raise InterruptedError("sequence cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("task deadline exceeded")

    def _observe(self, run_dir, after_ns=0):
        observation = self.backend.observe(run_dir, after_ns=after_ns, cancel=self.cancel_event)
        stamp = observation.get("metadata", {}).get("server_monotonic_ns")
        if type(stamp) is not int or stamp <= after_ns:
            raise ContractError("backend returned stale observation evidence")
        self._check()
        return observation

    def _step(self, plan, step, revision, run_dir):
        self._check()
        with self._lock:
            self._state.update(current_step=step.step_id, instruction=step.instruction)
        self._event(run_dir, "step_started", step_id=step.step_id, instruction=step.instruction)
        deadline = time.monotonic() + step.max_duration_ns / 1e9
        # Reject unsupported contexts before preflight or any action.
        context = self.compiler.compile(step, f"{plan.plan_id}:{step.step_id}", revision)
        before = self._observe(run_dir)
        precondition = self.monitor.begin(step, before, cancel=self.cancel_event)
        self._check(deadline)
        self._event(
            run_dir, "precondition_checked", step_id=step.step_id, verdict=precondition, observation=before["metadata"]
        )
        if precondition.get("verdict") == "blocked":
            raise TaskBlocked(precondition["reason"], {"observation": before, "verdict": precondition})
        if precondition.get("verdict") != "continue":
            raise RuntimeError("task precondition was not confirmed")
        units = 0
        while True:
            self._check(deadline)
            batch = self.settings.units_per_check
            if units + batch > self.settings.max_units_per_step:
                raise TimeoutError(f"{step.step_id}: chunk budget exhausted without verified success")
            self._active_operation = uuid.uuid4().hex
            result = self.backend.execute(
                self._active_operation, context, batch, min(deadline - time.monotonic(), 600), cancel=self.cancel_event
            )
            if result.get("state") != "completed":
                raise RuntimeError(result.get("reason", "action batch failed"))
            if type(result.get("finished_monotonic_ns")) is not int or result["finished_monotonic_ns"] <= 0:
                raise ContractError("backend returned an invalid execution timestamp")
            self._active_operation = None
            self._check(deadline)
            units += batch
            self._event(
                run_dir,
                "batch_completed",
                step_id=step.step_id,
                chunks=units,
                completion_signal=result.get("completion_signal", {}),
                context_receipt=result.get("receipt"),
            )
            if not self.monitor.should_check(result, units):
                continue
            after = self._observe(run_dir, result["finished_monotonic_ns"])
            verdict = self.monitor.evaluate(step, before, after, result, cancel=self.cancel_event)
            self._check(deadline)
            self._event(
                run_dir,
                "verification",
                step_id=step.step_id,
                chunks=units,
                verdict=verdict,
                observation=after["metadata"],
                context_receipt=result.get("receipt"),
            )
            status = verdict.get("verdict")
            if status == "blocked":
                if result.get("handoff_ready") is not True:
                    raise RuntimeError("blocked task is not ready for replanning handoff")
                raise TaskBlocked(verdict["reason"], {"observation": after, "verdict": verdict})
            if status == "continue":
                continue
            if status != "succeeded":
                raise RuntimeError("task needs attention: " + str(verdict.get("reason", "invalid verdict")))
            # Goal verification never authorizes switching during an unfinished action batch.
            if result.get("handoff_ready", False) is not True:
                raise RuntimeError("goal achieved but backend is not ready for handoff")
            for _ in range(self.settings.confirmations - 1):
                after = self._observe(
                    run_dir, after["metadata"]["server_monotonic_ns"] + self.settings.confirmation_interval_ns
                )
                confirmation = self.monitor.evaluate(step, before, after, result, cancel=self.cancel_event)
                self._check(deadline)
                self._event(
                    run_dir,
                    "completion_check",
                    step_id=step.step_id,
                    verdict=confirmation,
                    observation=after["metadata"],
                )
                if confirmation.get("verdict") != "succeeded":
                    raise RuntimeError("task completion was not confirmed: " + str(confirmation.get("reason", "")))
            return

    def run(self, plan):
        # Revalidate externally produced plans before they can select a journal path.
        plan = Plan.from_dict(plan.to_dict())
        with self._lock:
            if self._active:
                raise RuntimeError("a sequence is already active")
            self._active = True
            self._state = {"status": "running", "plan_id": plan.plan_id, "completed_steps": [], "replans": 0}
        run_dir = self.output_dir / (plan.plan_id + "-" + uuid.uuid4().hex[:8])
        try:
            run_dir.mkdir()
            (run_dir / "plan.json").write_text(
                json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._check()
            self._event(
                run_dir,
                "run_started",
                settings=vars(self.settings),
                components={
                    "backend": type(self.backend).__name__,
                    "monitor": type(self.monitor).__name__,
                    "compiler": type(self.compiler).__name__,
                },
            )
            self.backend.check_capabilities()
            pending, completed = deque(plan.steps), []
            revision, attempts = 0, 0
            while pending:
                step = pending.popleft()
                revision += 1
                attempts += 1
                if attempts > 64:
                    raise RuntimeError("run step budget exhausted")
                try:
                    self._step(plan, step, revision, run_dir)
                except TaskBlocked as exc:
                    self._check()
                    count = self.snapshot()["replans"]
                    if self.replanner is None or count >= self.settings.max_replans:
                        raise
                    self._event(run_dir, "replan_requested", step_id=step.step_id, evidence=exc.evidence)
                    revised = self.replanner.revise(
                        plan, tuple(completed), step, exc.evidence, cancel=self.cancel_event
                    )
                    self._check()
                    revised = Plan.from_dict(revised.to_dict())
                    if revised.request_id != plan.request_id or revised.mission != plan.mission:
                        raise ContractError("replanning cannot change the original mission") from exc
                    if {s.step_id for s in completed} & {s.step_id for s in revised.steps}:
                        raise ContractError("replanning cannot requeue completed step identifiers") from exc
                    pending = deque(revised.steps)
                    with self._lock:
                        self._state["replans"] = count + 1
                    self._event(run_dir, "plan_revised", plan=revised.to_dict())
                    continue
                completed.append(step)
                with self._lock:
                    self._state["completed_steps"] = [s.step_id for s in completed]
                self._event(run_dir, "step_succeeded", step_id=step.step_id)
            self._check()
            with self._lock:
                self._state["status"] = "succeeded"
        except Exception as exc:
            if self._active_operation is not None:
                try:
                    self.backend.cancel(self._active_operation)
                except Exception as cancel_error:
                    if run_dir.is_dir():
                        self._event(run_dir, "cancel_error", reason=str(cancel_error))
                self._active_operation = None
            with self._lock:
                cancelled = self.cancel_event.is_set() or isinstance(exc, InterruptedError)
                self._state.update(status="cancelled" if cancelled else "failed", reason=str(exc))
        finally:
            with self._lock:
                self._state["output_dir"] = str(run_dir)
            result = self.snapshot()
            try:
                if run_dir.is_dir():
                    (run_dir / "result.json").write_text(
                        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    self._event(run_dir, "run_finished", result=result)
            finally:
                with self._lock:
                    self._active = False
        return result
