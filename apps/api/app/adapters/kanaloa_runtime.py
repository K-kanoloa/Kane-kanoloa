"""Kanaloa Agent Runtime.

Owns agent runtime-level execution capabilities:
1. Session-Scoped Persistent IPython (stateful variables, calculation, data manipulation).
2. OS Shell Execution (PowerShell on Windows, Bash on Unix).
3. Optional Loop Mode (governed strictly by single field max_iterations: int | None).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
import sys
import types
from typing import Any, Literal

from IPython.core.interactiveshell import InteractiveShell
from IPython.utils.io import capture_output

logger = logging.getLogger(__name__)


@dataclass
class IPythonExecutionResult:
    stdout: str
    stderr: str
    result: Any
    success: bool
    error: str | None = None


class SessionIPythonRuntime:
    """
    Session-scoped persistent IPython execution environment.
    Guarantees state persistence across multiple turns within the same session,
    while maintaining strict variable and namespace isolation from other sessions.
    """

    _clean_base_ns: dict[str, Any] | None = None

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        # Obtain global InteractiveShell instance
        self._shell = InteractiveShell.instance()

        if SessionIPythonRuntime._clean_base_ns is None:
            SessionIPythonRuntime._clean_base_ns = {
                k: v for k, v in self._shell.user_ns.items()
                if not k.startswith("_i") and k not in ("_", "__", "___", "_dh")
            }

        # Create isolated module for this session
        mod_name = f"kanaloa_session_{session_id.replace('-', '_')}"
        self.user_module = types.ModuleType(mod_name)
        # Populate with standard base builtins and initial namespace
        self.user_module.__dict__.update(dict(SessionIPythonRuntime._clean_base_ns))
        self.user_module.__dict__["__name__"] = mod_name
        self.user_module.__dict__["__session_id__"] = session_id
        self.user_module.__dict__["In"] = [""]
        self.user_module.__dict__["Out"] = {}
        self.user_module.__dict__["_oh"] = self.user_module.__dict__["Out"]
        self._is_active = True

    def execute(self, code: str) -> IPythonExecutionResult:
        """Execute Python code within this session's persistent namespace."""
        if not self._is_active:
            raise RuntimeError(f"IPython runtime for session '{self.session_id}' has been closed.")

        # Activate this session's module and namespace in the shell
        self._shell.user_module = self.user_module
        self._shell.user_ns = self.user_module.__dict__

        with capture_output() as cap:
            run_res = self._shell.run_cell(code)

        stdout = cap.stdout
        stderr = cap.stderr
        error_msg = None
        if run_res.error_in_exec:
            error_msg = str(run_res.error_in_exec)

        return IPythonExecutionResult(
            stdout=stdout,
            stderr=stderr,
            result=run_res.result,
            success=run_res.success,
            error=error_msg,
        )

    def get_variable(self, name: str) -> Any:
        """Read a variable from this session's namespace."""
        if not self._is_active:
            raise RuntimeError(f"IPython runtime for session '{self.session_id}' has been closed.")
        return self.user_module.__dict__.get(name)

    def close(self) -> None:
        """Release session namespace and clean up."""
        self._is_active = False
        # Clear user namespace dictionary to free memory and references
        self.user_module.__dict__.clear()


class SessionShellRuntime:
    """
    OS-Native Shell Execution:
    - Windows: PowerShell (powershell.exe -NoProfile -NonInteractive -Command ...)
    - Linux/macOS: Bash (/bin/bash -c ...)
    """

    @staticmethod
    def get_shell_type() -> Literal["powershell", "bash"]:
        return "powershell" if sys.platform == "win32" else "bash"

    @classmethod
    async def execute(
        cls,
        command: str,
        cwd: str | None = None,
        timeout: float = 60.0,
    ) -> tuple[int, str, str]:
        """Execute a shell command using the platform-native shell."""
        if sys.platform == "win32":
            shell_cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
        else:
            shell_cmd = ["/bin/bash", "-c", command]

        proc = await asyncio.create_subprocess_exec(
            *shell_cmd,
            cwd=cwd or os.getcwd(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            exit_code = proc.returncode or 0
            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            return exit_code, stdout, stderr
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return -1, "", f"Command timed out after {timeout} seconds"


class KanaloaRuntime:
    """
    Kanaloa Agent Runtime Lifecycle Owner:
    - Manages session-scoped IPython runtimes.
    - Manages OS-native shell calls.
    - Coordinates optional Loop Mode without modifying Kane Core.
    """

    def __init__(self) -> None:
        self._ipython_sessions: dict[str, SessionIPythonRuntime] = {}
        self.shell = SessionShellRuntime()

    def get_ipython(self, session_id: str) -> SessionIPythonRuntime:
        """Get or create session-scoped persistent IPython runtime."""
        if session_id not in self._ipython_sessions:
            self._ipython_sessions[session_id] = SessionIPythonRuntime(session_id)
        return self._ipython_sessions[session_id]

    def close_ipython_session(self, session_id: str) -> None:
        """Close and release IPython runtime for a closed session."""
        session = self._ipython_sessions.pop(session_id, None)
        if session:
            session.close()

    def close_all(self) -> None:
        """Release all active sessions on shutdown."""
        for session in list(self._ipython_sessions.values()):
            session.close()
        self._ipython_sessions.clear()
