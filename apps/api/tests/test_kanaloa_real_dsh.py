"""Opt-in integration check against the bundled DSH ACP process."""

from __future__ import annotations

import asyncio
import os
import json
import subprocess
from pathlib import Path

import pytest

from app.adapters.kanaloa_adapter import KanaloaAdapter
from app.domain.models import Conversation, Message, Turn
from app.harness.coordinator import HarnessCoordinator
from app.harness.mailbox import MailboxManager
from app.store.sqlite_store import SQLiteStore


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("KANE_REAL_DSH_TEST") != "1", reason="requires configured DSH provider")
async def test_bundled_dsh_prompt_has_native_completion_truth():
    repo_root = Path(__file__).resolve().parents[3]
    store = SQLiteStore(":memory:")
    coordinator = HarnessCoordinator(store, MailboxManager())
    adapter = KanaloaAdapter(cwd=str(repo_root), event_handler=coordinator)
    native_results = {}
    original_send_request = adapter._send_request

    async def capture_native_result(method, params=None):
        response = await original_send_request(method, params)
        if method in {"session/new", "session/prompt"}:
            native_results[method] = response.get("result", {})
        return response

    adapter._send_request = capture_native_result
    coordinator.register_adapter("kanaloa", adapter)
    conversation = Conversation(conversation_id="real_dsh_completion", bound_agent_id="kanaloa")
    turn = Turn(turn_id="real_dsh_turn", conversation_id=conversation.conversation_id, bound_agent_id="kanaloa")
    store.save_conversation(conversation)
    store.save_turn(turn)

    try:
        await adapter.send(
            turn,
            Message(conversation_id=conversation.conversation_id, sender="user", content="Reply with PONG only. Do not use tools."),
            [],
        )
        expected_model = os.getenv("KANE_REAL_DSH_MODEL")
        selected_models = [option["currentValue"] for option in native_results["session/new"]["configOptions"] if option.get("category") == "model"]
        print("Native session model:", selected_models)
        if expected_model:
            assert any(expected_model in json.loads(value) for value in selected_models)
        async with asyncio.timeout(120):
            while store.get_turn(turn.turn_id).status == "running":
                await asyncio.sleep(0.1)

        assert adapter.last_stop_reason == "end_turn"
        assert native_results["session/prompt"]["_meta"]["kaneNativeEndKind"] == "completed"
        assert store.get_turn(turn.turn_id).status == "finished"
        messages = store.get_messages(conversation.conversation_id)
        assert len(messages) == 1
        assert messages[0].sender == "agent"
        assert "PONG" in messages[0].content
        await asyncio.sleep(1)
        assert adapter.is_alive(), "DSH exited after completion"
        print(f"ACCEPTANCE PONG session={turn.native_session_ref} status=finished native=completed agent_messages=1 process_alive=true")
    finally:
        process = adapter._process
        await adapter.close()
        store.close()
        if process is not None:
            assert process.returncode == 0, "DSH did not close gracefully"


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("KANE_REAL_DSH_TEST") != "1", reason="requires bundled DSH")
async def test_unconfigured_runtime_refuses_fixed_model_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "unconfigured-runtime"))
    repo_root = Path(__file__).resolve().parents[3]
    adapter = KanaloaAdapter(cwd=str(repo_root))
    try:
        await adapter.activate()
        response = await adapter._send_request("session/new", {"cwd": str(repo_root), "mcpServers": []})
        assert response["error"]["code"] == -32602
        assert "not configured" in response["error"]["message"]
        assert not adapter._active_sessions
    finally:
        await adapter.close()


@pytest.fixture
def isolated_real_runtime(tmp_path, monkeypatch):
    """Reuse the user's saved route in an isolated Runtime; never print its secret."""
    repo_root = Path(__file__).resolve().parents[3]
    runtime_home = tmp_path / "runtime"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    seed = '''
import fs from "node:fs";
import YAML from "yaml";
import {resolveSpec as settingsSpec} from "@deepseek-ai/dsh-settings-file";
import {resolveSpec as credentialsSpec} from "@deepseek-ai/dsh-credentials-local";
const settings = YAML.parse(fs.readFileSync(settingsSpec({}).filename, "utf8"));
const credentials = YAML.parse(fs.readFileSync(credentialsSpec({}).filename, "utf8"));
const selection = settings["agent-default-model"];
const route = settings["llm-pi-ai"].providers[selection.provider];
const key = credentials.refs[route.apiKeyEnv];
if (!key) throw new Error("test_credential_missing");
const home = process.argv[1];
fs.mkdirSync(home, {recursive:true});
fs.writeFileSync(home + "/settings.yaml", JSON.stringify({"agent-default-model":selection,"llm-pi-ai":{providers:{[selection.provider]:route}}}));
fs.writeFileSync(home + "/.credentials.yaml", JSON.stringify({version:1,refs:{[route.apiKeyEnv]:key}}), {mode:0o600});
'''
    subprocess.run(["node", "--input-type=module", "-e", seed, str(runtime_home)], cwd=repo_root, check=True, capture_output=True)
    monkeypatch.setenv("DSH_HOME", str(runtime_home))
    monkeypatch.setenv("DSH_TELEMETRY_DISABLED", "1")
    try:
        yield runtime_home, workspace
    finally:
        (runtime_home / ".credentials.yaml").unlink(missing_ok=True)


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("KANE_REAL_DSH_TEST") != "1", reason="requires configured Kanaloa provider")
async def test_real_kanaloa_identity(isolated_real_runtime):
    _, workspace = isolated_real_runtime
    store = SQLiteStore(":memory:")
    coordinator = HarnessCoordinator(store, MailboxManager())
    adapter = KanaloaAdapter(cwd=str(workspace), event_handler=coordinator)
    coordinator.register_adapter("kanaloa", adapter)
    conversation = Conversation()
    turn = Turn(conversation_id=conversation.conversation_id, bound_agent_id="kanaloa")
    store.save_conversation(conversation)
    store.save_turn(turn)
    try:
        await adapter.send(turn, Message(conversation_id=conversation.conversation_id, sender="user", content="What is your Agent name, and what harness are you running in? Explain its foundation briefly. Do not use tools."), [])
        async with asyncio.timeout(120):
            while store.get_turn(turn.turn_id).status == "running":
                await asyncio.sleep(0.1)
        assert store.get_turn(turn.turn_id).status == "finished"
        messages = store.get_messages(conversation.conversation_id)
        assert len(messages) == 1
        assert "kanaloa" in messages[0].content.lower()
        print(f"ACCEPTANCE identity session={turn.native_session_ref} status=finished identity=kanaloa")
        assert adapter.is_alive()
    finally:
        await adapter.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("KANE_REAL_DSH_TEST") != "1" or os.name != "nt", reason="opt-in Windows Runtime crash check")
@pytest.mark.parametrize("mode", ["tool", "foreground", "background"])
async def test_real_crash_preserves_checkpoint_without_rerunning_work(isolated_real_runtime, tmp_path, mode):
    runtime_home, workspace = isolated_real_runtime
    store = SQLiteStore(tmp_path / "crash.db")
    coordinator = HarnessCoordinator(store, MailboxManager())
    adapter = KanaloaAdapter(cwd=str(workspace), event_handler=coordinator)
    coordinator.register_adapter("kanaloa", adapter)
    conversation = Conversation(conversation_id=f"real_crash_{mode}", bound_agent_id="kanaloa")
    turn = Turn(turn_id=f"crash_{mode}", conversation_id=conversation.conversation_id, bound_agent_id="kanaloa")
    store.save_conversation(conversation)
    store.save_turn(turn)
    marker = workspace / "execution.txt"
    approvals = 0
    command = f"Add-Content -LiteralPath '{marker}' -Value 'STARTED'; Start-Sleep -Seconds 60; Add-Content -LiteralPath '{marker}' -Value 'DONE'"
    work = f"Use pwsh exactly once, run_in_background=false, command: {command}. Do not use any other tools."
    if mode == "tool":
        prompt = work + " Wait for the command to finish, then reply DONE."
    else:
        prompt = f"Call subagent exactly once with run_in_background={'true' if mode == 'background' else 'false'}, description='isolated lifecycle test', prompt={json.dumps(work)}. "
        prompt += "After delegation returns, reply PARENT_DONE immediately. Do not call job_output, send_message, or any other tools."

    try:
        await adapter.send(turn, Message(conversation_id=conversation.conversation_id, sender="user", content=prompt), [])
        async with asyncio.timeout(240):
            while not marker.exists() or (mode == "background" and store.get_turn(turn.turn_id).status != "finished"):
                for permission in adapter.list_pending_permissions():
                    assert store.get_turn(turn.turn_id).status == "waiting_user"
                    await adapter.respond_permission(permission.request_id, "allow-once", permission.session_id)
                    approvals += 1
                assert store.get_turn(turn.turn_id).status not in {"failed", "interrupted"}, "Runtime failed before crash injection"
                await asyncio.sleep(0.1)
        before = store.get_turn(turn.turn_id)
        assert before.status == ("finished" if mode == "background" else "running")
        assert marker.read_text(encoding="utf-8-sig").count("STARTED") == 1
        assert "DONE" not in marker.read_text(encoding="utf-8-sig")
        assert any(event.event_type == "tool_start" for event in store.list_events(turn.turn_id))
        if mode != "tool":
            checkpoint = _native_checkpoint_facts(runtime_home)
            assert "subagent" in checkpoint["origins"]
            assert ("continuable" if mode == "background" else "one-shot") in checkpoint["child_modes"]

        process = adapter._process
        killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(process.pid), "/T", "/F", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        assert await killer.wait() == 0
        await process.wait()
        async with asyncio.timeout(10):
            while mode != "background" and store.get_turn(turn.turn_id).status != "interrupted":
                await asyncio.sleep(0.1)
        assert store.get_turn(turn.turn_id).status == ("finished" if mode == "background" else "interrupted")
        assert len(store.get_messages(conversation.conversation_id)) == (1 if mode == "background" else 0)
        partial = store.get_turn(turn.turn_id).partial_output
        session_ref = store.get_turn(turn.turn_id).native_session_ref
        await adapter.close()

        recovered = KanaloaAdapter(cwd=str(workspace), event_handler=coordinator)
        coordinator.register_adapter("kanaloa", recovered)
        assert not await recovered.probe_session(session_ref)
        await coordinator.reconcile_startup_turns()
        restored = store.get_turn(turn.turn_id)
        assert restored.partial_output == partial
        assert restored.status == ("finished" if mode == "background" else "interrupted")
        try:
            methods = []
            original_request = recovered._send_request

            async def record_request(method, params=None):
                methods.append(method)
                return await original_request(method, params)

            recovered._send_request = record_request
            if mode == "background":
                # The parent already completed; inspect its checkpoint without resuming its work.
                await recovered.activate()
                response = await recovered._send_request("session/resume", {"sessionId": session_ref, "cwd": str(workspace)})
                assert "error" not in response
            else:
                await recovered.resume(restored, [])
            await asyncio.sleep(1)
            assert restored.native_session_ref == session_ref
            assert "session/prompt" not in methods, "Session rebind must not submit unfinished work"
            assert marker.read_text(encoding="utf-8-sig").count("STARTED") == 1
            assert "DONE" not in marker.read_text(encoding="utf-8-sig")
            assert store.get_turn(turn.turn_id).status == ("finished" if mode == "background" else "interrupted")
            if mode != "background":
                assert _native_checkpoint_facts(runtime_home)["unknown_outcome"]
            print(f"ACCEPTANCE {mode}_crash session={session_ref} status={store.get_turn(turn.turn_id).status} partial_chars={len(partial or '')} session_rebind=PASS automatic_rerun=NO checkpoint_unknown_outcome={mode != 'background'} approval_count={approvals}")
        finally:
            await recovered.close()
    finally:
        await adapter.close()
        store.close()


def _native_checkpoint_facts(runtime_home):
    """Inspect native compressed checkpoints without exposing their contents."""
    script = '''
import fs from "node:fs";
import path from "node:path";
import {zstdDecompressSync} from "node:zlib";
import {Context} from "@deepseek-ai/cordis";
import JsonlSessionPersistence from "@deepseek-ai/dsh-session-persistence-jsonl";
const root = path.join(process.argv[1], "sessions");
const facts = {origins:[], child_modes:[], unknown_outcome:false};
const readers = new Map();
for (const entry of fs.readdirSync(root, {recursive:true})) {
  if (!entry.endsWith(".jsonl") && !entry.endsWith(".jsonl.zstd")) continue;
  const bytes = fs.readFileSync(path.join(root, entry));
  const text = (entry.endsWith(".zstd") ? zstdDecompressSync(bytes) : bytes).toString();
  const header = JSON.parse(text.split("\\n")[0]);
  facts.origins.push(header.origin);
  const compression = entry.endsWith(".zstd") ? "zstd" : "none";
  if (!readers.has(compression)) readers.set(compression, new JsonlSessionPersistence(new Context(), {root, compression}));
  const handle = await readers.get(compression).open(header.id, "read");
  try {
    const {events} = await handle.read();
    facts.unknown_outcome ||= events.some(event => event.data?.error?.code === "TOOL_OUTCOME_UNKNOWN");
    facts.child_modes.push(...events.filter(event => event.type === "subagent/descriptor").map(event => event.data.mode));
  } finally {
    await handle.close();
  }
}
console.log(JSON.stringify(facts));
'''
    repo_root = Path(__file__).resolve().parents[3]
    result = subprocess.run(["node", "--input-type=module", "-e", script, str(runtime_home)], cwd=repo_root, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)
