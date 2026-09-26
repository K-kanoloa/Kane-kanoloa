"""Kane vNext Turn Mailbox.

Disciplines:
- Causal Isolation: Mailbox is STRICTLY for INBOUND items entering a Turn:
  1. User messages (follow-ups or initial prompt)
  2. Steer inputs (for safe-boundary or follow-up steering)
  (Control signals like cancel/abort are out-of-band immediate calls and do NOT queue in Mailbox)
- Mailbox MUST NOT contain or buffer Agent Outbound Events (thinking, delta, stream_done).
  Outbound events bypass Mailbox entirely and flow through AgentEventHandler / EventStream.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import time
from typing import Any, Literal

from ..id_utils import new_id

InboundItemType = Literal["message", "steer"]


@dataclass
class MailboxItem:
    turn_id: str
    item_type: InboundItemType
    payload: dict[str, Any] = field(default_factory=dict)
    item_id: str = field(default_factory=lambda: new_id("mbx"))
    created_at: str = field(
        default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )


class TurnMailbox:
    """Per-turn inbound async queue."""

    def __init__(self, turn_id: str) -> None:
        self.turn_id = turn_id
        self._queue: asyncio.Queue[MailboxItem] = asyncio.Queue()

    async def put(self, item: MailboxItem) -> None:
        """Enqueue an inbound item for this turn."""
        if item.turn_id != self.turn_id:
            raise ValueError(
                f"Cannot put item for turn {item.turn_id} into mailbox for {self.turn_id}"
            )
        await self._queue.put(item)

    def put_nowait(self, item: MailboxItem) -> None:
        """Enqueue an inbound item synchronously/non-blocking."""
        if item.turn_id != self.turn_id:
            raise ValueError(
                f"Cannot put item for turn {item.turn_id} into mailbox for {self.turn_id}"
            )
        self._queue.put_nowait(item)

    async def get(self, timeout: float | None = None) -> MailboxItem | None:
        """Retrieve the next inbound item, optionally waiting up to timeout seconds."""
        if timeout is None:
            return await self._queue.get()
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def get_nowait(self) -> MailboxItem | None:
        """Retrieve next inbound item without waiting, or None if empty."""
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    def peek_pending(self) -> list[MailboxItem]:
        """Inspect all currently pending items in the queue without consuming them."""
        # Queue doesn't expose public peek, but we can copy the underlying deque
        return list(self._queue._queue)

    def drain_all(self) -> list[MailboxItem]:
        """Drain and return all currently pending items."""
        items: list[MailboxItem] = []
        while not self._queue.empty():
            try:
                items.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return items

    @property
    def is_empty(self) -> bool:
        return self._queue.empty()

    @property
    def pending_count(self) -> int:
        return self._queue.qsize()


class MailboxManager:
    """Central registry of TurnMailboxes."""

    def __init__(self) -> None:
        self._mailboxes: dict[str, TurnMailbox] = {}

    def get_mailbox(self, turn_id: str) -> TurnMailbox:
        if turn_id not in self._mailboxes:
            self._mailboxes[turn_id] = TurnMailbox(turn_id)
        return self._mailboxes[turn_id]

    def remove_mailbox(self, turn_id: str) -> TurnMailbox | None:
        return self._mailboxes.pop(turn_id, None)

    def clear(self) -> None:
        self._mailboxes.clear()
