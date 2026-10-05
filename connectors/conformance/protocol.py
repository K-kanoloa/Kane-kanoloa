from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from pathlib import Path
import re
from typing import Any


PROTOCOL = "kane-connector"
VERSION = "0.1"
ROOT = Path(__file__).resolve().parents[2]
SCHEMA_MARKER = re.compile(r"```json capabilities-schema\s*(.*?)\s*```", re.DOTALL)


class ConformanceError(ValueError):
    pass


def _fail(message: str) -> None:
    raise ConformanceError(message)


def _frame(record: Any, expected_direction: str) -> Mapping[str, Any]:
    if not isinstance(record, Mapping) or record.get("direction") != expected_direction:
        _fail(f"expected trace direction {expected_direction}")
    frame = record.get("frame")
    if not isinstance(frame, Mapping):
        _fail("trace frame must be an object")
    if frame.get("protocol") != PROTOCOL or frame.get("version") != VERSION:
        _fail("unsupported protocol envelope")
    if not isinstance(frame.get("id"), str) or not frame["id"]:
        _fail("frame id is required")
    if not isinstance(frame.get("type"), str):
        _fail("frame type is required")
    if not isinstance(frame.get("payload"), Mapping):
        _fail("frame payload must be an object")
    return frame


def _capabilities(value: Any) -> None:
    if not isinstance(value, Mapping):
        _fail("connector.hello must declare capabilities")
    source = (ROOT / "docs" / "KANE_CONNECTOR_PROTOCOL.md").read_text(encoding="utf-8")
    match = SCHEMA_MARKER.search(source)
    if not match:
        _fail("canonical capabilities schema is missing from the protocol document")
    schema = json.loads(match.group(1))
    if schema.get("additionalProperties") is False and set(value) - set(schema.get("properties", {})):
        _fail("capability declaration has unknown fields")
    missing = set(schema.get("required", [])) - set(value)
    if missing:
        _fail(f"capability declaration missing fields: {', '.join(sorted(missing))}")
    python_types = {"boolean": bool, "integer": int, "string": str, "null": type(None)}
    for key, rules in schema.get("properties", {}).items():
        if key not in value:
            continue
        allowed = rules.get("type")
        if allowed:
            allowed = [allowed] if isinstance(allowed, str) else allowed
            if not any(type(value[key]) is python_types.get(kind) for kind in allowed):
                _fail(f"invalid capability type: {key}")
        if "enum" in rules and value[key] not in rules["enum"]:
            _fail(f"invalid capability value: {key}")
        if value[key] is not None and "minimum" in rules and value[key] < rules["minimum"]:
            _fail(f"capability below minimum: {key}")


def check_trace(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Validate a redacted Connector wire trace against baseline v0.1 behavior."""
    if isinstance(records, (str, bytes)) or not isinstance(records, Sequence):
        _fail("trace must be an array")

    outbound = [r for r in records if isinstance(r, Mapping) and r.get("direction") == "connector_to_kane"]
    inbound = [r for r in records if isinstance(r, Mapping) and r.get("direction") == "kane_to_connector"]
    transport = [r for r in records if isinstance(r, Mapping) and r.get("direction") == "transport"]
    observations = [r for r in records if isinstance(r, Mapping) and r.get("direction") == "kane_observation"]
    if len(outbound) + len(inbound) + len(transport) + len(observations) != len(records):
        _fail("each trace record must have a supported direction")
    for record in [*outbound, *inbound]:
        _frame(record, str(record.get("direction")))
    hello_frames = [_frame(r, "connector_to_kane") for r in outbound if (r.get("frame") or {}).get("type") == "connector.hello"]
    ready_frames = [_frame(r, "kane_to_connector") for r in inbound if (r.get("frame") or {}).get("type") == "connector.ready"]
    if len(hello_frames) != 2 or len(ready_frames) != 2:
        _fail("trace must show initial and reconnect handshakes")
    transport_types = [r.get("frame", {}).get("type") if isinstance(r.get("frame"), Mapping) else None for r in transport]
    if len(transport) != 2 or transport_types != ["disconnect", "reconnect"]:
        _fail("trace must show one disconnect followed by one reconnect")
    if transport[0]["frame"].get("agent_status") != "unavailable" or transport[1]["frame"].get("agent_status") != "ready":
        _fail("registry must reflect unavailable on disconnect and ready after reconnect")
    reconnect_record = next(r for r in outbound if r.get("frame") is hello_frames[-1])
    disconnect_record = transport[0]
    disconnect_index = next(i for i, record in enumerate(records) if record is disconnect_record)
    reconnect_index = next(i for i, record in enumerate(records) if record is reconnect_record)
    if disconnect_index >= reconnect_index:
        _fail("reconnect handshake must follow the transport disconnect")

    first_hello, reconnect_hello = hello_frames[0], hello_frames[-1]
    first_hello_record = next(r for r in outbound if r.get("frame") is first_hello)
    first_ready = ready_frames[0]
    first_ready_record = next(r for r in inbound if r.get("frame") is first_ready)
    if records.index(first_hello_record) >= records.index(first_ready_record):
        _fail("connector.ready must follow connector.hello")
    identity = first_hello["payload"]
    if not isinstance(identity.get("agent_id"), str) or not identity["agent_id"]:
        _fail("agent_id is required")
    if not isinstance(identity.get("connector_id"), str) or not identity["connector_id"]:
        _fail("connector_id is required")
    _capabilities(identity.get("capabilities"))
    if reconnect_hello["payload"].get("agent_id") != identity["agent_id"]:
        _fail("agent identity changed on reconnect")
    if reconnect_hello["payload"].get("connector_id") != identity["connector_id"]:
        _fail("connector identity changed on reconnect")
    for ready in ready_frames:
        if ready["payload"].get("agent_id") != identity["agent_id"]:
            _fail("connector.ready agent identity mismatch")

    send_frames = [_frame(r, "kane_to_connector") for r in inbound if (r.get("frame") or {}).get("type") == "turn.send"]
    if not send_frames:
        _fail("trace must contain a turn.send")
    send = send_frames[0]
    send_payload = send["payload"]
    if not isinstance(send.get("request_id"), str) or not send.get("request_id"):
        _fail("turn.send requires request_id")
    turn_id = send_payload.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id:
        _fail("turn.send requires turn_id")
    request_id = send.get("request_id")
    accepted = False
    for record in outbound:
        frame = _frame(record, "connector_to_kane")
        if frame.get("type") == "command.result" and frame.get("request_id") == request_id:
            accepted = frame["payload"].get("status") == "accepted"
            break
    if not accepted:
        _fail("turn.send must have a matching accepted command.result")
    send_record = next(r for r in inbound if r.get("frame") is send)
    ack_record = next(r for r in outbound if r.get("frame") is not None and r["frame"].get("type") == "command.result" and r["frame"].get("request_id") == request_id)
    if records.index(send_record) >= records.index(ack_record):
        _fail("command.result must follow the delivered turn.send")

    events: list[Mapping[str, Any]] = []
    for record in outbound:
        frame = _frame(record, "connector_to_kane")
        if frame.get("type") in {"session.bound", "reply.delta", "reply.completed"}:
            events.append(frame)
    bound_refs = [f["payload"].get("native_session_ref") for f in events if f.get("type") == "session.bound"]
    completed = [f for f in events if f.get("type") == "reply.completed"]
    deltas = [f for f in events if f.get("type") == "reply.delta"]
    if len(completed) != 1:
        _fail("one logical reply must end with exactly one reply.completed")
    for frame in events:
        if frame.get("type") in {"session.bound", "reply.delta", "reply.completed"} and not isinstance(frame.get("event_id"), str):
            _fail(f"{frame['type']} requires event_id")
    reply_id = completed[0]["payload"].get("reply_id")
    if not isinstance(reply_id, str) or not reply_id:
        _fail("reply.completed requires reply_id")
    for frame in [*deltas, *completed]:
        payload = frame["payload"]
        if payload.get("turn_id") != turn_id or payload.get("reply_id") != reply_id:
            _fail("all deltas and completion must belong to the same logical reply")
        if frame.get("type") == "reply.delta" and not isinstance(payload.get("text"), str):
            _fail("reply.delta requires text")
    complete_record = next(r for r in outbound if r.get("frame") is completed[0])
    if records.index(ack_record) >= records.index(complete_record):
        _fail("reply completion must follow accepted delivery")

    session_ref = send_payload.get("native_session_ref") or (bound_refs[-1] if bound_refs else None)
    if not isinstance(session_ref, str) or not session_ref:
        _fail("trace must establish a native session reference")
    reconnect_sessions = reconnect_hello["payload"].get("sessions")
    if not isinstance(reconnect_sessions, list) or session_ref not in reconnect_sessions:
        _fail("reconnect must report the existing native session reference")
    if len(observations) != 1:
        _fail("trace must include one Kane persistence observation")
    observation = observations[0].get("frame")
    if not isinstance(observation, Mapping) or observation.get("type") != "turn_state":
        _fail("Kane persistence observation must be a turn_state object")
    if observation.get("turn_id") != turn_id or observation.get("status") != "finished":
        _fail("persisted Turn must be finished for the tested reply")
    if observation.get("agent_message_count") != 1:
        _fail("one logical Agent reply must persist as exactly one Kane Message")

    return {
        "status": "pass",
        "agent_id": identity["agent_id"],
        "connector_id": identity["connector_id"],
        "turn_id": turn_id,
        "reply_id": reply_id,
        "delta_count": len(deltas),
        "completion_count": 1,
        "session_continuity": True,
        "reconnected": True,
        "disconnect_reflected_truthfully": True,
        "persisted_agent_message_count": 1,
        "scope": "wire trace only; native Agent execution is not proven",
    }
