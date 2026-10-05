from __future__ import annotations

import unittest

from protocol import ConformanceError, check_trace


def frame(direction, kind, payload, **fields):
    return {"direction": direction, "frame": {
        "protocol": "kane-connector", "version": "0.1", "type": kind,
        "id": f"id-{kind}", "payload": payload, **fields,
    }}


def trace():
    capabilities = {
        "supports_stream": True, "supports_resume": False,
        "supports_cancel": False, "supports_approval": False,
        "supports_parallel_sessions": False, "max_parallel_sessions": None,
        "steer_mode": "follow_up_only", "branch_mode": "unsupported",
    }
    identity = {"agent_id": "agent-1", "connector_id": "install-1", "capabilities": capabilities, "sessions": []}
    reconnect_identity = {**identity, "sessions": ["session-1"]}
    return [
        frame("connector_to_kane", "connector.hello", identity),
        frame("kane_to_connector", "connector.ready", {"agent_id": "agent-1"}),
        frame("kane_to_connector", "turn.send", {"turn_id": "turn-1", "content": "ping"}, request_id="request-1"),
        frame("connector_to_kane", "command.result", {"status": "accepted"}, request_id="request-1"),
        frame("connector_to_kane", "session.bound", {"turn_id": "turn-1", "native_session_ref": "session-1"}, event_id="event-bound"),
        frame("connector_to_kane", "reply.delta", {"turn_id": "turn-1", "reply_id": "reply-1", "text": "PONG"}, event_id="event-delta"),
        frame("connector_to_kane", "reply.completed", {"turn_id": "turn-1", "reply_id": "reply-1"}, event_id="event-complete"),
        {"direction": "transport", "frame": {"type": "disconnect", "agent_status": "unavailable"}},
        {"direction": "transport", "frame": {"type": "reconnect", "agent_status": "ready"}},
        frame("connector_to_kane", "connector.hello", reconnect_identity),
        frame("kane_to_connector", "connector.ready", {"agent_id": "agent-1"}),
        {"direction": "kane_observation", "frame": {"type": "turn_state", "turn_id": "turn-1", "status": "finished", "agent_message_count": 1}},
    ]


class ConnectorConformanceTests(unittest.TestCase):
    def test_baseline_trace_passes_with_one_logical_reply_and_session_reconnect(self):
        result = check_trace(trace())
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["completion_count"], 1)
        self.assertTrue(result["session_continuity"])

    def test_missing_explicit_completion_fails(self):
        records = trace()
        records.pop(6)
        with self.assertRaisesRegex(ConformanceError, "reply.completed"):
            check_trace(records)

    def test_duplicate_completion_fails(self):
        records = trace()
        records.insert(7, dict(records[6]))
        with self.assertRaisesRegex(ConformanceError, "exactly one"):
            check_trace(records)

    def test_reconnect_must_preserve_session(self):
        records = trace()
        records[9]["frame"]["payload"]["sessions"] = []
        with self.assertRaisesRegex(ConformanceError, "native session"):
            check_trace(records)

    def test_capability_values_are_checked_against_canonical_schema(self):
        records = trace()
        records[0]["frame"]["payload"]["capabilities"]["branch_mode"] = "pretend-native"
        with self.assertRaisesRegex(ConformanceError, "branch_mode"):
            check_trace(records)

    def test_disconnect_must_be_reflected_as_unavailable(self):
        records = trace()
        records[7]["frame"]["agent_status"] = "ready"
        with self.assertRaisesRegex(ConformanceError, "unavailable"):
            check_trace(records)

    def test_persisted_message_count_must_remain_one(self):
        records = trace()
        records[-1]["frame"]["agent_message_count"] = 2
        with self.assertRaisesRegex(ConformanceError, "exactly one Kane Message"):
            check_trace(records)


if __name__ == "__main__":
    unittest.main()
