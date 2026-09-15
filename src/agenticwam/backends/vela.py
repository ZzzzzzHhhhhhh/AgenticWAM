"""HTTP client for bounded Vela execution and fresh camera evidence."""

from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from agenticwam.backends.legacy_wire import WamContext
from agenticwam.backends.vela_signals import completion_settings
from agenticwam.core.types import ContractError as ContractViolation


class VelaContextBackend:
    def __init__(self, endpoint, *, camera_roles=("front_view", "wrist_view"), http_timeout_sec=5, completion=None):
        address = urlsplit(endpoint)
        if address.scheme not in {"http", "https"} or not address.hostname or address.username or address.password:
            raise ContractViolation("console endpoint must be an HTTP(S) URL without embedded credentials")
        if address.query or address.fragment:
            raise ContractViolation("console endpoint cannot contain query parameters or a fragment")
        self.endpoint = endpoint.rstrip("/")
        self.camera_roles = tuple(camera_roles)
        if not self.camera_roles or len(set(self.camera_roles)) != len(self.camera_roles):
            raise ContractViolation("declare distinct observation camera roles")
        if any(
            not isinstance(role, str)
            or not role
            or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in role)
            for role in self.camera_roles
        ):
            raise ContractViolation("camera roles must be alphanumeric identifiers")
        self.http_timeout_sec = http_timeout_sec
        self.cancel_epoch = None
        self.completion = completion_settings(completion)

    def _request(self, path, value=None, *, image=False):
        data = None if value is None else json.dumps(value, ensure_ascii=False).encode("utf-8")
        request = Request(self.endpoint + path, data=data, headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=self.http_timeout_sec) as response:
            content = response.read(4 * 1024 * 1024 + 1)
            if len(content) > 4 * 1024 * 1024:
                raise ContractViolation("console response exceeds its budget")
            if image:
                if response.headers.get_content_type() != "image/jpeg":
                    raise ContractViolation("camera preview must be JPEG")
                return content
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ContractViolation("console returned an invalid response")
            return result

    def snapshot(self):
        return self._request("/api/v1/snapshot")

    def check_capabilities(self):
        caps = self._request("/api/v1/capabilities")
        names = {entry["type"] for entry in caps.get("commands", [])}
        if not {"agent.execute", "agent.cancel"} <= names:
            raise ContractViolation("console does not provide the versioned agent execution interface")
        snapshot = self.snapshot()
        agent = snapshot.get("agent", {})
        if agent.get("schema") != "vela.agent.execution/v1" or agent.get("active"):
            raise ContractViolation("agent execution interface is unavailable or already active")
        self.cancel_epoch = agent["cancel_epoch"]
        if self.completion["type"] == "gripper_release" and not agent.get("gripper_signal_supported"):
            raise ContractViolation("profile requires measured gripper release events, which this runtime lacks")
        return caps

    def _check_cancelled(self, snapshot, cancel):
        if cancel is not None and cancel.is_set():
            raise InterruptedError("sequence cancelled")
        if snapshot.get("agent", {}).get("cancel_epoch") != self.cancel_epoch:
            raise InterruptedError("operator stopped this sequence")

    def _intent(self, command, payload, *, command_id=None):
        outcome = self._request(
            "/api/v1/intents",
            {
                "type": command,
                "payload": payload,
                "id": command_id or uuid.uuid4().hex,
                "source": "context-agent",
            },
        )
        deadline = time.monotonic() + self.http_timeout_sec
        while outcome["status"] in {"queued", "running"}:
            if time.monotonic() >= deadline:
                raise TimeoutError("console did not consume the command before its deadline")
            time.sleep(0.05)
            outcome = self._request("/api/v1/commands/" + quote(outcome["command_id"], safe=""))
        if outcome["status"] != "succeeded":
            raise ContractViolation(outcome.get("message", "console rejected command"))
        return outcome.get("details", {})

    def execute(self, operation_id, context, chunks, timeout_sec, *, cancel=None):
        context = WamContext(context.context_id, context.revision, context.text)
        self._check_cancelled(self.snapshot(), cancel)
        self._intent(
            "agent.execute",
            {
                "operation_id": operation_id,
                "context": context.to_dict(),
                "chunks": chunks,
                "timeout_sec": timeout_sec,
                "cancel_epoch": self.cancel_epoch,
                "completion": self.completion,
            },
            command_id=operation_id,
        )
        deadline = time.monotonic() + timeout_sec + self.http_timeout_sec
        while time.monotonic() < deadline:
            snapshot = self.snapshot()
            failure = snapshot.get("agent", {})
            if (
                failure.get("operation_id") == operation_id
                and failure.get("state") == "failed"
                and not failure.get("active")
                and not (cancel is not None and cancel.is_set())
            ):
                return failure
            self._check_cancelled(snapshot, cancel)
            state = snapshot.get("agent", {})
            if state.get("operation_id") != operation_id:
                raise ContractViolation("console execution identity changed unexpectedly")
            if not state["active"]:
                if state.get("state") == "completed":
                    context.verify_receipt((state.get("receipt") or {}).get("task_context"))
                    state = {**state, "handoff_ready": True}
                return state
            time.sleep(0.05)
        raise TimeoutError("bounded action execution did not return")

    def cancel(self, operation_id):
        return self._intent("agent.cancel", {"operation_id": operation_id})

    def observe(self, directory, *, after_ns=0, cancel=None):
        started = time.monotonic()
        deadline = started + self.http_timeout_sec
        while True:
            snapshot = self.snapshot()
            self._check_cancelled(snapshot, cancel)
            now = snapshot["updated_monotonic_ns"]
            streams = {row["role"]: row for row in snapshot.get("cameras", {}).get("streams", [])}
            valid = all(
                role in streams
                and after_ns < streams[role].get("monotonic_ns", 0) <= now
                and now - streams[role]["monotonic_ns"] <= 500_000_000
                for role in self.camera_roles
            )
            if valid:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("required cameras did not provide fresh post-action evidence")
            time.sleep(0.05)
        observation_id = uuid.uuid4().hex
        snapshot_seconds = time.monotonic() - started
        transfer_started = time.monotonic()

        def download(role):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("sequence cancelled")
            requested = time.monotonic()
            data = self._request("/api/v1/previews/" + quote(role, safe=""), image=True)
            return data, {"role": role, "bytes": len(data), "elapsed_seconds": time.monotonic() - requested}

        # Camera reads are independent; map preserves the declared image/camera order.
        with ThreadPoolExecutor(max_workers=min(4, len(self.camera_roles))) as pool:
            previews = list(pool.map(download, self.camera_roles))
        transfer_seconds = time.monotonic() - transfer_started
        if cancel is not None and cancel.is_set():
            raise InterruptedError("sequence cancelled")
        images = []
        for index, (data, _) in enumerate(previews):
            path = directory / f"{observation_id}-{index}.jpg"
            path.write_bytes(data)
            images.append(str(path))
        metadata = {
            "observation_id": observation_id,
            "server_monotonic_ns": now,
            "image_order": list(self.camera_roles),
            "camera_frames": [streams[r] for r in self.camera_roles],
            "hardware": snapshot.get("hardware", {}),
        }
        return {
            "images": images,
            "metadata": metadata,
            "diagnostics": {
                "snapshot_wait_seconds": snapshot_seconds,
                "preview_transfer_seconds": transfer_seconds,
                "image_bytes": sum(len(data) for data, _ in previews),
                "previews": [metrics for _, metrics in previews],
            },
        }
