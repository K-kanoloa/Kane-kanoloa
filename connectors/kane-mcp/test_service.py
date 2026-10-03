from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
from service import (  # noqa: E402
    get_capabilities_schema,
    get_connection_info,
    get_connection_guide,
    get_connector_spec,
    run_conformance_check,
)


def compliant_trace():
    def wire(direction, kind, payload, **extra):
        return {"direction": direction, "frame": {
            "protocol": "kane-connector", "version": "0.1", "type": kind,
            "id": kind, "payload": payload, **extra,
        }}

    caps = {
        "supports_stream": True, "supports_resume": False, "supports_cancel": False,
        "supports_approval": False, "supports_parallel_sessions": False,
        "max_parallel_sessions": None, "steer_mode": "follow_up_only",
        "branch_mode": "unsupported",
    }
    hello = {"connector_id": "install", "agent_id": "sample", "capabilities": caps, "sessions": []}
    reconnect = {**hello, "sessions": ["native-1"]}
    return [
        wire("connector_to_kane", "connector.hello", hello),
        wire("kane_to_connector", "connector.ready", {"agent_id": "sample"}),
        wire("kane_to_connector", "turn.send", {"turn_id": "turn-1", "native_session_ref": "native-1"}, request_id="send-1"),
        wire("connector_to_kane", "command.result", {"status": "accepted"}, request_id="send-1"),
        wire("connector_to_kane", "session.bound", {"turn_id": "turn-1", "native_session_ref": "native-1"}, event_id="bound-1"),
        wire("connector_to_kane", "reply.delta", {"turn_id": "turn-1", "reply_id": "reply-1", "text": "PONG"}, event_id="delta-1"),
        wire("connector_to_kane", "reply.completed", {"turn_id": "turn-1", "reply_id": "reply-1"}, event_id="complete-1"),
        {"direction": "transport", "frame": {"type": "disconnect", "agent_status": "unavailable"}},
        {"direction": "transport", "frame": {"type": "reconnect", "agent_status": "ready"}},
        wire("connector_to_kane", "connector.hello", reconnect),
        wire("kane_to_connector", "connector.ready", {"agent_id": "sample"}),
        {"direction": "kane_observation", "frame": {"type": "turn_state", "turn_id": "turn-1", "status": "finished", "agent_message_count": 1}},
    ]


class MCPServiceTests(unittest.TestCase):
    def test_guide_reads_shared_skill_without_runtime_credentials(self):
        with patch.dict("os.environ", {"KANE_API_TOKEN": "private-test-token"}, clear=True):
            result = get_connection_guide()
        self.assertEqual(result["protocol_source"], get_connector_spec()["path"])
        for guide in result["guides"]:
            self.assertEqual(guide["content"], (ROOT / guide["path"]).read_text(encoding="utf-8"))
        self.assertNotIn("private-test-token", json.dumps(result))

    def test_protocol_and_capabilities_come_from_one_document(self):
        spec = get_connector_spec()
        schema = get_capabilities_schema()
        self.assertIn("Kane Connector Protocol v0.1", spec["spec"])
        self.assertEqual(schema["source"], spec["path"])
        self.assertIn("supports_stream", schema["schema"]["properties"])

    def test_connection_info_never_returns_configured_secret(self):
        with patch.dict("os.environ", {
            "KANE_API_BASE_URL": "https://kane.example", "KANE_API_TOKEN": "secret-value",
            "KANE_AGENT_ID": "agent-one",
        }, clear=True):
            result = get_connection_info()
        self.assertEqual(result["connector_websocket_url"], "wss://kane.example/api/v1/connectors/ws")
        self.assertTrue(result["api_token_configured"])
        self.assertNotIn("secret-value", json.dumps(result))

    def test_shared_conformance_accepts_and_rejects_trace(self):
        trace = compliant_trace()
        passed = run_conformance_check(json.dumps(trace))
        self.assertEqual(passed["status"], "pass")
        self.assertTrue(passed["session_continuity"])
        trace[6]["frame"]["type"] = "turn.finished"
        failed = run_conformance_check(json.dumps(trace))
        self.assertEqual(failed["status"], "fail")


if __name__ == "__main__":
    unittest.main()
