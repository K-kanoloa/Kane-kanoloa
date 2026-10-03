"""Kanaloa private optional Loop execution. Native tools remain in DSH."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def normalize_acp_outcome(stop_reason: Any, native_kind: Any) -> tuple[str, str, str]:
    stop = stop_reason if stop_reason in ("end_turn", "cancelled", "failed", "max_tokens") else "unknown"
    native = native_kind if native_kind in ("completed", "blocked", "error", "aborted", "interrupted", "max-tokens") else "unknown"
    if stop == "cancelled" or native in ("aborted", "interrupted"):
        outcome = "cancelled"
    elif native in ("blocked", "error") or stop == "failed":
        outcome = "failed"
    elif stop == "end_turn" and native == "completed":
        outcome = "completed"
    else:
        outcome = "unknown"
    return stop, native, outcome


class KanaloaRuntime:
    """Owns only the optional Kanaloa Loop policy, not native tool execution."""

    def __init__(self) -> None:
        self._active_loops: dict[str, dict[str, Any]] = {}
        self._iteration_outputs: dict[str, list[str]] = {}

    def close_all(self) -> None:
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
        accumulated_text = ""

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

                # Check if turn was externally marked inactive (interrupted, failed, or finished)
                if hasattr(event_handler, "is_turn_active") and not event_handler.is_turn_active(turn_id):
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
                    error_code = resp["error"].get("code", "unknown")
                    error_code = error_code if isinstance(error_code, int) else "unknown"
                    await event_handler.emit_interrupted(turn_id, reason=f"acp_protocol:{error_code}")
                    return

                # Check if cancelled mid-flight
                native_kind = resp.get("result", {}).get("_meta", {}).get("kaneNativeEndKind")
                stop_reason, native_kind, outcome = normalize_acp_outcome(stop_reason, native_kind)
                logger.info(
                    "KanaloaRuntime: iteration outcome turn=%s session=%s stop=%s native=%s",
                    turn_id, session_id, stop_reason, native_kind,
                )
                if outcome == "cancelled" or loop_meta.get("cancelled"):
                    await event_handler.emit_interrupted(turn_id, reason="cancelled_by_acp")
                    return
                if outcome == "failed":
                    await event_handler.emit_failed(turn_id, reason="agent_reported_failure")
                    return
                if outcome != "completed":
                    await event_handler.emit_interrupted(
                        turn_id, reason=f"acp_stop:{stop_reason or 'missing'}:native_{native_kind or 'missing'}"
                    )
                    return

                # Check if turn was externally marked inactive during iteration execution
                if hasattr(event_handler, "is_turn_active") and not event_handler.is_turn_active(turn_id):
                    logger.info("KanaloaRuntime: turn %s is no longer active, halting loop", turn_id)
                    return

                # 3. Check for early completion: strict COMPLETE marker
                iteration_text = "".join(self._iteration_outputs.get(turn_id, []))
                accumulated_text += iteration_text

                if self.is_complete_marker(iteration_text) or self.is_complete_marker(accumulated_text):
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
            logger.error("KanaloaRuntime: loop execution exception: %s", type(e).__name__)
            if not hasattr(event_handler, "is_turn_active") or event_handler.is_turn_active(turn_id):
                await event_handler.emit_interrupted(turn_id, reason=f"acp_transport:{type(e).__name__}")
        finally:
            self._active_loops.pop(turn_id, None)
            self._iteration_outputs.pop(turn_id, None)
