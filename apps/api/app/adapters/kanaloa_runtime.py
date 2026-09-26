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
import re
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
    - Manages session-scoped persistent IPython runtimes.
    - Manages OS-native shell calls (PowerShell on Windows, Bash on Unix).
    - Owns optional Loop Mode execution & orchestration (iteration counting, COMPLETE determination, stop conditions).
    """

    def __init__(self) -> None:
        self._ipython_sessions: dict[str, SessionIPythonRuntime] = {}
        self.shell = SessionShellRuntime()
        self._active_loops: dict[str, dict[str, Any]] = {}
        self._iteration_outputs: dict[str, list[str]] = {}

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
        """Release all active sessions and abort active loops on shutdown."""
        for session in list(self._ipython_sessions.values()):
            session.close()
        self._ipython_sessions.clear()
        self.cancel_all_loops()

    # --- Loop Mode Management & Orchestration (Owned by KanaloaRuntime) ---

    @staticmethod
    def is_complete_marker(text: str) -> bool:
        """
        Strict, non-fragile COMPLETE marker detection.
        Rejects false-positives such as 'not COMPLETE yet', 'COMPLETE condition not met'.
        Matches explicit bracketed token [COMPLETE], standalone line COMPLETE,
        or sentence-final isolated COMPLETE token.
        """
        if not text or not isinstance(text, str):
            return False
        # 1. Match explicit bracketed token [COMPLETE] anywhere as a distinct token
        if re.search(r"\[COMPLETE\]", text, re.IGNORECASE):
            return True
        # 2. Match standalone COMPLETE on its own line (optional markdown bold/code/punctuation)
        for line in text.splitlines():
            stripped = line.strip().strip("*_`~# \t").rstrip(".").rstrip("!")
            if stripped.upper() == "COMPLETE":
                return True
        # 3. Match sentence-final isolated COMPLETE token after punctuation or newline
        if re.search(r"(?:^|[\r\n]|(?<=[.!?])\s+)COMPLETE[.!]?\s*$", text.strip(), re.IGNORECASE):
            return True
        return False

    def record_delta(self, turn_id: str, text: str) -> None:
        """Record streaming delta for active loop output tracking."""
        if turn_id in self._iteration_outputs:
            self._iteration_outputs[turn_id].append(text)

    def record_steer(self, turn_id: str, steer_text: str) -> None:
        """Record steering input for active loop without incrementing iteration."""
        if turn_id in self._active_loops:
            self._active_loops[turn_id]["pending_steer"] = steer_text

    def stop_loop(self, turn_id: str) -> None:
        """Gracefully stop continuing loop. Current iteration finishes, next does not start."""
        if turn_id in self._active_loops:
            self._active_loops[turn_id]["stopped"] = True

    def cancel_loop(self, turn_id: str) -> None:
        """Immediately abort active loop. Interrupted outcome."""
        if turn_id in self._active_loops:
            self._active_loops[turn_id]["cancelled"] = True

    def cancel_all_loops(self) -> None:
        """Abort all active loops on shutdown or disconnect."""
        for loop_meta in self._active_loops.values():
            loop_meta["cancelled"] = True
        self._active_loops.clear()
        self._iteration_outputs.clear()

    def get_loop_iteration(self, turn_id: str) -> int:
        """Get current iteration count for an active loop."""
        loop_meta = self._active_loops.get(turn_id)
        return loop_meta["current_iteration"] if loop_meta else 0

    def is_loop_active(self, turn_id: str) -> bool:
        """Check if a loop is actively running for the given turn_id."""
        return turn_id in self._active_loops

    async def execute_loop(
        self,
        turn: Any,
        session_id: str,
        initial_prompt_blocks: list[dict[str, Any]],
        max_iterations: int | None,
        send_prompt_fn: Any,
        event_handler: Any,
    ) -> None:
        """
        Optional Loop Mode orchestration in KanaloaRuntime.
        Governed strictly by single field max_iterations: int | None.
        1 iteration = 1 complete Kanaloa/DSH work-cycle.
        Neither approval nor steer increments iteration count.
        Stop conditions:
        - COMPLETE (strict non-fragile marker or structured signal)
        - Stop (graceful stop requested by caller)
        - Cancel (cancelled by caller or ACP)
        - failed (error or failure reported)
        - interrupted (Turn status interrupted)
        - max_iterations reached (for Default=5 or Custom=N)
        """
        turn_id = turn.turn_id
        current_prompt = initial_prompt_blocks
        iteration = 0
        loop_meta: dict[str, Any] = {
            "session_id": session_id,
            "max_iterations": max_iterations,
            "current_iteration": 0,
            "stopped": False,
            "cancelled": False,
            "pending_steer": None,
        }
        self._active_loops[turn_id] = loop_meta

        try:
            while True:
                # 1. Check if cancelled before starting iteration
                if loop_meta.get("cancelled"):
                    await event_handler.emit_interrupted(turn_id, reason="cancelled")
                    return

                # Check if gracefully stopped before starting iteration
                if loop_meta.get("stopped"):
                    logger.info("KanaloaRuntime: loop stopped before iteration %d for turn %s", iteration + 1, turn_id)
                    break

                # 2. Check turn status in store before starting iteration
                store = getattr(event_handler, "store", None)
                if store:
                    t_pre = store.get_turn(turn_id)
                    if t_pre and t_pre.status in ("interrupted", "failed"):
                        return

                # Start 1 full work-cycle
                iteration += 1
                loop_meta["current_iteration"] = iteration
                logger.info(
                    "KanaloaRuntime: starting iteration %d (max: %s) for turn %s (session %s)",
                    iteration,
                    max_iterations,
                    turn_id,
                    session_id,
                )

                # Reset iteration output accumulator
                self._iteration_outputs[turn_id] = []

                # Execute one work-cycle via Adapter transport
                resp, stop_reason = await send_prompt_fn(session_id, current_prompt)

                if "error" in resp:
                    err_msg = resp["error"].get("message", "ACP prompt error in loop")
                    await event_handler.emit_failed(turn_id, reason=str(err_msg))
                    return

                # Check if cancelled mid-flight
                if stop_reason == "cancelled" or loop_meta.get("cancelled"):
                    await event_handler.emit_interrupted(turn_id, reason="cancelled_by_acp")
                    return

                # Check if turn was interrupted or failed during this iteration (e.g. disconnect)
                if store:
                    t_post = store.get_turn(turn_id)
                    if t_post and t_post.status in ("interrupted", "failed"):
                        logger.info("KanaloaRuntime: turn %s entered %s, halting loop", turn_id, t_post.status)
                        return

                # 3. Check for early completion: strict COMPLETE marker
                iteration_text = "".join(self._iteration_outputs.get(turn_id, []))
                turn_obj = store.get_turn(turn_id) if store else turn
                full_output = (turn_obj.partial_output or "") if turn_obj else ""

                if self.is_complete_marker(iteration_text) or self.is_complete_marker(full_output):
                    logger.info(
                        "KanaloaRuntime: loop early complete detected at iteration %d for turn %s",
                        iteration,
                        turn_id,
                    )
                    break

                # 4. Check if gracefully stopped during this iteration
                if loop_meta.get("stopped"):
                    logger.info("KanaloaRuntime: loop stopped at iteration %d for turn %s", iteration, turn_id)
                    break

                # 5. Check if max_iterations limit reached (for Default=5 or Custom=N)
                if max_iterations is not None and iteration >= max_iterations:
                    logger.info(
                        "KanaloaRuntime: loop reached max_iterations (%d) for turn %s",
                        max_iterations,
                        turn_id,
                    )
                    break

                # 6. Prepare prompt for next iteration (work-cycle)
                # If steer was received during iteration, use steered content; steer does NOT add iteration!
                pending_steer = loop_meta.pop("pending_steer", None)
                if pending_steer:
                    current_prompt = [{"type": "text", "text": pending_steer}]
                else:
                    current_prompt = [{
                        "type": "text",
                        "text": "Continue with next step. Output '[COMPLETE]' when finished.",
                    }]

            # Loop finished normally (complete, stopped, or max_iterations reached)
            await event_handler.emit_message_complete(
                turn_id=turn_id,
                sender_id="kanaloa",
            )
        except Exception as e:
            logger.error("KanaloaRuntime: loop execution exception: %s", e)
            await event_handler.emit_failed(turn_id, reason=str(e))
        finally:
            self._active_loops.pop(turn_id, None)
            self._iteration_outputs.pop(turn_id, None)
