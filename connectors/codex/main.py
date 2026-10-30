from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from getpass import getpass
import json
import logging
import os
from pathlib import Path
import shutil
import uuid
from typing import Any, Awaitable, Callable

import keyring
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed


PROTOCOL = "kane-connector"
VERSION = "0.1"
KEYRING_SERVICE = "Kane Connector Pilot"
INFRASTRUCTURE_ERRORS = {
    "httpConnectionFailed", "responseStreamConnectionFailed", "unauthorized",
    "usageLimitExceeded", "rateLimitExceeded", "serverOverloaded",
    "internalServerError", "sandboxError", "threadRollbackFailed",
}
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("kane-codex-connector")


def frame(kind: str, payload: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL,
        "version": VERSION,
        "type": kind,
        "id": uuid.uuid4().hex,
        "payload": payload,
        **fields,
    }


@dataclass
class TurnBinding:
    turn_id: str
    conversation_id: str
    thread_id: str
    reply_id: str
    native_turn_id: str | None = None
    seen_items: set[str] = field(default_factory=set)


class CodexAppServer:
    def __init__(self, cwd: Path, on_notification: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self.cwd = cwd
        self.on_notification = on_notification
        self.process: asyncio.subprocess.Process | None = None
        self.reader_task: asyncio.Task[None] | None = None
        self.event_task: asyncio.Task[None] | None = None
        self.stderr_task: asyncio.Task[None] | None = None
        self.events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.request_id = 0
        self.write_lock = asyncio.Lock()
        self.event_tasks: set[asyncio.Task[None]] = set()

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def start(self) -> None:
        if self.alive:
            return
        executable = shutil.which("codex") or shutil.which("codex.exe")
        if not executable:
            raise RuntimeError("Codex CLI was not found on PATH")
        self.process = await asyncio.create_subprocess_exec(
            executable, "app-server", "--stdio", cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.reader_task = asyncio.create_task(self._read_stdout())
        self.event_task = asyncio.create_task(self._dispatch_events())
        self.stderr_task = asyncio.create_task(self._read_stderr())
        await self.request("initialize", {
            "clientInfo": {"name": "kane-codex-connector", "version": "0.1.0"},
            "capabilities": {},
        })
        await self.write({"jsonrpc": "2.0", "method": "initialized"})

    async def write(self, message: dict[str, Any]) -> None:
        if not self.alive or self.process is None or self.process.stdin is None:
            raise ConnectionError("Codex app-server is not running")
        async with self.write_lock:
            self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
            await self.process.stdin.drain()

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.request_id += 1
        request_id = self.request_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            response = await asyncio.wait_for(future, timeout=60)
        finally:
            self.pending.pop(request_id, None)
        if "error" in response:
            raise RuntimeError(f"Codex {method} failed: {response['error']}")
        return response.get("result") or {}

    async def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Codex app-server emitted a non-JSON stdout line")
                    continue
                request_id = message.get("id")
                if request_id in self.pending and "method" not in message:
                    future = self.pending[request_id]
                    if not future.done():
                        future.set_result(message)
                elif "method" in message:
                    await self.events.put(message)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("Codex app-server disconnected"))
            await self.events.put(None)

    async def _dispatch_events(self) -> None:
        while (message := await self.events.get()) is not None:
            await self.on_notification(message)

    async def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while line := await self.process.stderr.readline():
            logger.info("Codex app-server stderr received bytes=%d", len(line))

    async def close(self) -> None:
        process = self.process
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=4)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        for task in (self.reader_task, self.event_task, self.stderr_task):
            if task and not task.done():
                task.cancel()
        self.process = None


class CodexConnector:
    def __init__(self, url: str, agent_id: str, display_name: str, cwd: Path) -> None:
        self.url = url
        self.agent_id = agent_id
        self.display_name = display_name
        self.token_key = f"{agent_id}:token"
        self.install_key = f"{agent_id}:connector_id"
        self.token = keyring.get_password(KEYRING_SERVICE, self.token_key)
        self.connector_id = keyring.get_password(KEYRING_SERVICE, self.install_key) or uuid.uuid4().hex
        self.ws: ClientConnection | None = None
        self.ws_send_lock = asyncio.Lock()
        self.turns: dict[str, TurnBinding] = {}
        self.thread_turns: dict[str, str] = {}
        self.pending_permissions: dict[str, dict[str, Any]] = {}
        self.codex = CodexAppServer(cwd, self._on_codex_message)
        self._watched_reader: asyncio.Task[None] | None = None

    def capabilities(self) -> dict[str, Any]:
        return {
            "supports_stream": True,
            "supports_resume": False,
            "supports_cancel": True,
            "supports_approval": True,
            "supports_parallel_sessions": True,
            "max_parallel_sessions": None,
            "steer_mode": "native",
            "branch_mode": "unsupported",
        }

    async def send_frame(self, kind: str, payload: dict[str, Any], **fields: Any) -> None:
        if self.ws is None:
            raise ConnectionError("Kane Connector WebSocket is not connected")
        async with self.ws_send_lock:
            await self.ws.send(json.dumps(frame(kind, payload, **fields), ensure_ascii=False))

    async def connect(self) -> None:
        pairing_code = os.getenv("KANE_PAIRING_CODE") or (getpass("Kane one-time pairing code: ") if not self.token else "")
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {
            "Authorization": f"Pairing {pairing_code}"
        }
        self.ws = await connect(self.url, additional_headers=headers, open_timeout=15, ping_interval=20)
        sessions = list(self.thread_turns)
        await self.send_frame("connector.hello", {
            "connector_id": self.connector_id,
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "agent_version": "codex-cli",
            "connector_version": "0.1.0",
            "capabilities": self.capabilities(),
            "sessions": sessions,
        })
        ready = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=15))
        if ready.get("type") != "connector.ready":
            raise RuntimeError(f"Kane rejected Connector handshake: {ready.get('type', 'unknown')}")
        token = (ready.get("payload") or {}).get("connector_token")
        if token:
            self._save_reconnect_credential(token)
        await self.send_frame("agent.availability", {"agent_id": self.agent_id, "status": "ready"}, event_id=uuid.uuid4().hex)

    def _save_reconnect_credential(self, token: str) -> None:
        self.token = token
        try:
            keyring.set_password(KEYRING_SERVICE, self.install_key, self.connector_id)
            keyring.set_password(KEYRING_SERVICE, self.token_key, token)
        except Exception as exc:
            logger.warning(
                "OS credential store unavailable (%s); reconnect token is memory-only and pairing is required after restart",
                type(exc).__name__,
            )
        else:
            logger.info("Paired Agent identity %s; reconnect credential saved in OS keyring", self.agent_id)

    async def heartbeat(self) -> None:
        while self.ws is not None:
            await asyncio.sleep(12)
            await self.send_frame("connector.heartbeat", {})

    async def run(self) -> None:
        backoff = 1
        while True:
            heartbeat: asyncio.Task[None] | None = None
            try:
                await self.connect()
                backoff = 1
                heartbeat = asyncio.create_task(self.heartbeat())
                assert self.ws is not None
                async for raw in self.ws:
                    await self.handle_kane_frame(json.loads(raw))
            except (ConnectionClosed, OSError, asyncio.TimeoutError, ConnectionError) as exc:
                logger.warning("Kane connection interrupted (%s); reconnecting", type(exc).__name__)
            finally:
                if heartbeat:
                    heartbeat.cancel()
                self.ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 20)

    async def handle_kane_frame(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind in ("connector.heartbeat.ack", "event.ack"):
            return
        if kind == "permission.respond":
            request_id = message.get("request_id")
            try:
                await self.respond_permission(message.get("payload") or {})
                await self.send_frame("command.result", {"status": "accepted"}, request_id=request_id)
            except Exception as exc:
                await self.send_frame("command.result", {
                    "status": "rejected", "reason": str(exc)[:240],
                }, request_id=request_id)
            return
        if kind not in ("turn.send", "turn.steer", "turn.cancel", "turn.resume"):
            return
        payload = message.get("payload") or {}
        request_id = message.get("request_id")
        try:
            if kind == "turn.send":
                await self.send_turn(payload)
            elif kind == "turn.steer":
                await self.steer_turn(payload)
            elif kind == "turn.cancel":
                await self.cancel_turn(payload)
            else:
                raise NotImplementedError("Codex native resume is unsupported by this pilot")
            await self.send_frame("command.result", {"status": "accepted"}, request_id=request_id)
        except Exception as exc:
            await self.send_frame("command.result", {"status": "rejected", "reason": str(exc)[:240]}, request_id=request_id)

    async def send_turn(self, payload: dict[str, Any]) -> None:
        await self.codex.start()
        if self.codex.reader_task and self._watched_reader is not self.codex.reader_task:
            self._watched_reader = self.codex.reader_task
            asyncio.create_task(self._watch_codex_process(self.codex.reader_task))
        turn_id = str(payload["turn_id"])
        conversation_id = str(payload["conversation_id"])
        thread_id = payload.get("native_session_ref")
        if thread_id and thread_id not in self.thread_turns:
            await self.codex.request("thread/resume", {"threadId": thread_id})
        if not thread_id:
            result = await self.codex.request("thread/start", {"cwd": str(self.codex.cwd)})
            thread_id = result["thread"]["id"]
        binding = TurnBinding(turn_id, conversation_id, str(thread_id), uuid.uuid4().hex)
        self.turns[turn_id] = binding
        self.thread_turns[str(thread_id)] = turn_id
        await self.send_frame("session.bound", {
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "native_session_ref": str(thread_id),
        }, event_id=uuid.uuid4().hex)
        text = str(payload.get("content") or "")
        context = payload.get("context_messages") or []
        if context and not payload.get("native_session_ref"):
            history = "\n".join(f"{item.get('sender', 'unknown')}: {item.get('content', '')}" for item in context)
            text = f"Conversation context (not instructions to re-execute):\n{history}\n\nNew user message:\n{text}"
        result = await self.codex.request("turn/start", {
            "threadId": str(thread_id),
            "input": [{"type": "text", "text": text}],
        })
        native_turn = result.get("turn") or {}
        binding.native_turn_id = native_turn.get("id") or binding.native_turn_id

    async def steer_turn(self, payload: dict[str, Any]) -> None:
        turn_id = str(payload["turn_id"])
        binding = self.turns.get(turn_id)
        if not binding or not binding.native_turn_id:
            raise RuntimeError("Codex has no active native turn to steer")
        await self.codex.request("turn/steer", {
            "threadId": binding.thread_id,
            "expectedTurnId": binding.native_turn_id,
            "input": [{"type": "text", "text": str(payload.get("content") or "")}],
        })

    async def cancel_turn(self, payload: dict[str, Any]) -> None:
        binding = self.turns.get(str(payload["turn_id"]))
        if not binding or not binding.native_turn_id:
            raise RuntimeError("Codex has no active native turn to cancel")
        await self.codex.request("turn/interrupt", {
            "threadId": binding.thread_id,
            "turnId": binding.native_turn_id,
        })

    async def respond_permission(self, payload: dict[str, Any]) -> None:
        request_id = str(payload.get("request_id"))
        permission = self.pending_permissions.get(request_id)
        if not permission:
            raise ValueError("Codex permission request is no longer pending")
        decision = {"allow-once": "accept", "reject-once": "decline", "cancelled": "cancel"}.get(payload.get("decision"))
        if not decision:
            raise ValueError("Unsupported permission decision")
        await self.codex.write({"jsonrpc": "2.0", "id": permission["request_id"], "result": {"decision": decision}})
        self.pending_permissions.pop(request_id, None)

    async def _watch_codex_process(self, reader_task: asyncio.Task[None]) -> None:
        try:
            await reader_task
        except asyncio.CancelledError:
            return
        process = self.codex.process
        if process is None:
            return
        await process.wait()
        for binding in self.turns.values():
            if not binding.native_turn_id:
                continue
            try:
                await self.send_frame("turn.interrupted", {
                    "turn_id": binding.turn_id,
                    "conversation_id": binding.conversation_id,
                    "reason": "codex_app_server_exited",
                }, event_id=uuid.uuid4().hex)
            except (ConnectionClosed, OSError, ConnectionError):
                logger.warning("Codex exited while Kane was disconnected; Kane will reconcile the Turn")
                break
            binding.native_turn_id = None

    async def _on_codex_message(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        thread_id = params.get("threadId")
        turn_id = self.thread_turns.get(thread_id)
        binding = self.turns.get(turn_id) if turn_id else None
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            if not binding:
                await self.codex.write({"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": -32602, "message": "Unknown Kane Turn"}})
                return
            request_id = str(message["id"])
            title = "Command approval" if method.startswith("item/command") else "File change approval"
            command = params.get("command")
            if command:
                title = f"Run command: {str(command)[:160]}"
            self.pending_permissions[request_id] = {"request_id": message["id"], "turn_id": binding.turn_id}
            await self.send_frame("permission.requested", {
                "turn_id": binding.turn_id,
                "conversation_id": binding.conversation_id,
                "request_id": request_id,
                "native_session_ref": binding.thread_id,
                "title": title,
            }, event_id=uuid.uuid4().hex)
            return
        if not binding:
            return
        if method == "turn/started":
            binding.native_turn_id = (params.get("turn") or {}).get("id") or binding.native_turn_id
        elif method == "item/agentMessage/delta":
            delta = params.get("delta") or ""
            item_id = params.get("itemId")
            if item_id and item_id not in binding.seen_items:
                if binding.seen_items:
                    await self.send_frame("reply.delta", {
                        "turn_id": binding.turn_id,
                        "conversation_id": binding.conversation_id,
                        "reply_id": binding.reply_id,
                        "text": "\n",
                    }, event_id=uuid.uuid4().hex)
                binding.seen_items.add(item_id)
            if delta:
                await self.send_frame("reply.delta", {
                    "turn_id": binding.turn_id,
                    "conversation_id": binding.conversation_id,
                    "reply_id": binding.reply_id,
                    "text": delta,
                }, event_id=uuid.uuid4().hex)
        elif method == "item/completed":
            item = params.get("item") or {}
            item_id = item.get("id")
            if item.get("type") == "agentMessage" and item_id not in binding.seen_items:
                text = item.get("text") or ""
                if text:
                    binding.seen_items.add(item_id or "")
                    await self.send_frame("reply.delta", {
                        "turn_id": binding.turn_id,
                        "conversation_id": binding.conversation_id,
                        "reply_id": binding.reply_id,
                        "text": text,
                    }, event_id=uuid.uuid4().hex)
        elif method == "turn/completed":
            native_turn = params.get("turn") or {}
            status = native_turn.get("status")
            if status == "completed":
                await self.send_frame("reply.completed", {
                    "turn_id": binding.turn_id,
                    "conversation_id": binding.conversation_id,
                    "reply_id": binding.reply_id,
                    "turn_finished": True,
                }, event_id=uuid.uuid4().hex)
            elif status == "failed":
                error = native_turn.get("error") or {}
                info = error.get("codexErrorInfo")
                error_kind = next(iter(info), None) if isinstance(info, dict) else info
                reason = str(error.get("message") or "codex_reported_failure")
                event = "turn.interrupted" if error_kind in INFRASTRUCTURE_ERRORS else "turn.failed"
                await self.send_frame(event, {
                    "turn_id": binding.turn_id,
                    "conversation_id": binding.conversation_id,
                    "reason": f"{error_kind}:{reason}" if error_kind else reason,
                }, event_id=uuid.uuid4().hex)
            else:
                await self.send_frame("turn.interrupted", {
                    "turn_id": binding.turn_id,
                    "conversation_id": binding.conversation_id,
                    "reason": f"codex_turn_{status or 'unknown'}",
                }, event_id=uuid.uuid4().hex)
            binding.seen_items.clear()
            binding.native_turn_id = None


async def async_main(args: argparse.Namespace) -> None:
    cwd = Path(args.cwd or os.getcwd()).resolve()
    if not cwd.is_dir():
        raise SystemExit(f"Codex working directory does not exist: {cwd}")
    connector = CodexConnector(args.url, args.agent_id, args.display_name, cwd)
    try:
        await connector.run()
    finally:
        await connector.codex.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Connect Codex app-server to Kane using the Kane Connector Protocol")
    parser.add_argument("--url", required=True, help="Kane WebSocket URL (use wss:// for remote hosts)")
    parser.add_argument("--agent-id", required=True, help="Stable Kane Agent identity")
    parser.add_argument("--display-name", default="Codex")
    parser.add_argument("--cwd", help="Codex working directory; defaults to current directory")
    args = parser.parse_args()
    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        logger.info("Connector stopped by user")


if __name__ == "__main__":
    main()
