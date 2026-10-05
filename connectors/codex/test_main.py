import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from main import CodexConnector, TurnBinding, frame


class FakeWebSocket:
    def __init__(self):
        self.frames = []

    async def send(self, raw):
        import json
        self.frames.append(json.loads(raw))


class FakeProcess:
    returncode = 1

    async def wait(self):
        return self.returncode


class ConnectorTests(unittest.IsolatedAsyncioTestCase):
    def connector(self):
        with patch("main.keyring.get_password", return_value=None):
            connector = CodexConnector("ws://localhost", "codex-test", "Codex Test", Path.cwd())
        connector.ws = FakeWebSocket()
        return connector

    async def test_frame_has_protocol_envelope_and_unique_id(self):
        first = frame("reply.delta", {"text": "a"})
        second = frame("reply.delta", {"text": "b"})
        self.assertEqual(first["protocol"], "kane-connector")
        self.assertEqual(first["version"], "0.1")
        self.assertNotEqual(first["id"], second["id"])

    async def test_permission_command_returns_result_for_kane_request(self):
        connector = self.connector()
        with patch.object(connector, "respond_permission", return_value=None) as respond:
            await connector.handle_kane_frame({
                "type": "permission.respond",
                "request_id": "request-7",
                "payload": {"request_id": "native-3", "decision": "allow-once"},
            })
        respond.assert_awaited_once()
        result = connector.ws.frames[-1]
        self.assertEqual(result["type"], "command.result")
        self.assertEqual(result["request_id"], "request-7")
        self.assertEqual(result["payload"]["status"], "accepted")

    async def test_credential_store_failure_keeps_token_only_in_memory(self):
        connector = self.connector()
        token = "opaque-test-token"
        with patch("main.keyring.set_password", side_effect=OSError("credential store unavailable")):
            connector._save_reconnect_credential(token)
        self.assertEqual(connector.token, token)

    async def test_codex_delta_and_completion_map_to_one_reply(self):
        connector = self.connector()
        binding = TurnBinding("turn-1", "conv-1", "thread-1", "reply-1")
        connector.turns[binding.turn_id] = binding
        connector.thread_turns[binding.thread_id] = binding.turn_id

        await connector._on_codex_message({
            "method": "item/agentMessage/delta",
            "params": {"threadId": "thread-1", "itemId": "item-1", "delta": "PONG"},
        })
        await connector._on_codex_message({
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {"id": "native-1", "status": "completed"}},
        })

        self.assertEqual([item["type"] for item in connector.ws.frames], ["reply.delta", "reply.completed"])
        self.assertEqual(connector.ws.frames[0]["payload"]["text"], "PONG")
        self.assertEqual(connector.ws.frames[0]["payload"]["reply_id"], "reply-1")
        self.assertEqual(connector.ws.frames[1]["payload"]["reply_id"], "reply-1")

    async def test_app_server_exit_interrupts_active_turn(self):
        connector = self.connector()
        binding = TurnBinding("turn-1", "conv-1", "thread-1", "reply-1", "native-1")
        connector.turns[binding.turn_id] = binding
        connector.codex.process = FakeProcess()

        await connector._watch_codex_process(asyncio.create_task(asyncio.sleep(0)))

        self.assertEqual(connector.ws.frames[0]["type"], "turn.interrupted")
        self.assertEqual(connector.ws.frames[0]["payload"]["reason"], "codex_app_server_exited")
        self.assertIsNone(binding.native_turn_id)

    async def test_app_server_stderr_does_not_log_untrusted_content(self):
        from main import CodexAppServer
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        server = CodexAppServer(Path.cwd(), AsyncMock())
        server.process = SimpleNamespace(stderr=SimpleNamespace(
            readline=AsyncMock(side_effect=[b"secret-token prompt tool-output\n", b""]),
        ))
        with self.assertLogs("kane-codex-connector", level="INFO") as logs:
            await server._read_stderr()
        rendered = " ".join(logs.output)
        self.assertNotIn("secret-token", rendered)
        self.assertNotIn("prompt", rendered)
        self.assertNotIn("tool-output", rendered)
        self.assertIn("bytes=", rendered)


if __name__ == "__main__":
    unittest.main()
