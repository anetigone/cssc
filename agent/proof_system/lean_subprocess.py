"""Subprocess execution helpers for the Lean checker.

``lake env lean`` spawns ``lean`` as a child process, so timeouts must tear
down the whole process tree rather than just the parent. The helpers here
handle platform-specific process-group setup and best-effort cleanup.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


def combine_output(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    """Concatenate stdout/stderr into a single string, tolerating bytes."""

    def as_text(value: str | bytes | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode(errors="replace")
        return value

    parts = [part for part in (as_text(stdout), as_text(stderr)) if part]
    return "\n".join(parts).strip()


def _popen_kwargs_for_process_group() -> dict[str, Any]:
    """Return platform-specific kwargs so we can kill the whole process tree.

    ``lake env lean`` spawns ``lean`` as a child; killing only ``lake`` on a
    timeout would orphan the expensive Lean process. Starting the subprocess in
    its own session / process group lets us tear down the entire tree.
    """
    if sys.platform == "win32":
        # CREATE_NEW_PROCESS_GROUP + CREATE_NO_WINDOW so Ctrl-Break reaches the
        # whole group and no console window flashes up.
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags |= subprocess.CREATE_NO_WINDOW
        return {"creationflags": creationflags}
    return {"start_new_session": True}


def kill_process_tree(process: subprocess.Popen[Any]) -> None:
    """Best-effort termination of a process and all of its children."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5.0,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            except (OSError, ValueError):
                pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        process.kill()


class ProcessGroupRunner:
    """Run a command in its own process group and kill the tree on timeout."""

    def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        encoding: str = "utf-8",
        errors: str = "replace",
    ) -> subprocess.CompletedProcess[str]:
        """Run the command, killing the whole process tree on timeout.

        Unlike ``subprocess.run``, this starts the checker in its own process
        group so a timeout tears down ``lake`` and the ``lean`` child it
        spawned.
        """
        popen = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding=encoding,
            errors=errors,
            **_popen_kwargs_for_process_group(),
        )
        try:
            stdout, stderr = popen.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            # Enforce the requested deadline first. Draining before termination
            # would let an expensive Lean process continue beyond its budget.
            partial_stdout = exc.output or ""
            partial_stderr = exc.stderr or ""
            kill_process_tree(popen)
            try:
                stdout, stderr = popen.communicate(timeout=5.0)
            except subprocess.TimeoutExpired:
                try:
                    popen.kill()
                except OSError:
                    pass
                stdout, stderr = popen.communicate()
            raise subprocess.TimeoutExpired(
                cmd=command,
                timeout=timeout_seconds,
                output=stdout or partial_stdout,
                stderr=stderr or partial_stderr,
            )
        return subprocess.CompletedProcess(
            args=command,
            returncode=popen.returncode if popen.returncode is not None else 0,
            stdout=stdout or "",
            stderr=stderr or "",
        )
