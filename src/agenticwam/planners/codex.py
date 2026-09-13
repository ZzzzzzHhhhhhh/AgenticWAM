"""Official Codex CLI structured-model adapter, isolated from robot execution."""

import json
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from agenticwam.core.types import ContractError as ContractViolation


class CodexJson:
    def __init__(self, *, executable="codex", model="gpt-6-astra", timeout_sec=120):
        self.executable, self.model, self.timeout_sec = executable, model, timeout_sec

    def ask(self, prompt, schema, *, images=(), cancel=None):
        with tempfile.TemporaryDirectory(prefix="agenticwam-codex-") as temporary:
            root = Path(temporary)
            schema_path, output = root / "schema.json", root / "answer.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            command = [
                self.executable,
                "exec",
                "--model",
                self.model,
                "--sandbox",
                "read-only",
                "--disable",
                "shell_tool",
                "--ignore-user-config",
                "--ephemeral",
                "--skip-git-repo-check",
                "--color",
                "never",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output),
                "-c",
                'web_search="disabled"',
                "-c",
                'model_reasoning_effort="low"',
                "-c",
                'model_provider="agenticwam_codex"',
                "-c",
                'model_providers.agenticwam_codex={name="Codex HTTPS", wire_api="responses", '
                "requires_openai_auth=true, supports_websockets=false}",
            ]
            for path in images:
                command.extend(["--image", str(Path(path).resolve())])
            command.append("-")
            with (root / "stderr.log").open("wb") as errors:
                process = subprocess.Popen(
                    command,
                    cwd=root,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=errors,
                    start_new_session=True,
                )
                try:
                    process.stdin.write(prompt.encode("utf-8"))
                    process.stdin.close()
                    deadline = time.monotonic() + self.timeout_sec
                    while process.poll() is None:
                        if cancel is not None and cancel.is_set():
                            raise InterruptedError("Codex request cancelled")
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Codex response timed out")
                        time.sleep(0.05)
                    if process.returncode != 0:
                        raise RuntimeError(
                            f"Codex exited with status {process.returncode}; check Codex login/model access"
                        )
                    if not output.is_file() or output.stat().st_size > 131072:
                        raise ContractViolation("Codex returned no bounded structured response")
                    value = json.loads(output.read_text(encoding="utf-8"))
                    if not isinstance(value, dict) or set(value) != set(schema["properties"]):
                        raise ContractViolation("Codex response does not match the requested object schema")
                    return value
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
