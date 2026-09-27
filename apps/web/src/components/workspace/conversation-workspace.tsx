"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Agent, type Conversation, type Message, type SendBody, type Turn } from "@/lib/api";
import { useT } from "@/lib/i18n/LocaleProvider";
import { useTurn } from "@/lib/use-turn";
import { SidebarNav } from "../sidebar-nav";
import { TopBar } from "../top-bar";
import { ChatPanel } from "./chat-panel";
import { Composer } from "./composer";
import { WorkInspector } from "./work-inspector";
import { BrandMark, ErrorNotice, IconButton, Loading, Modal, turnTitle } from "./ui";

type Dialog = { kind: "conversation" | "task" } | { kind: "branch"; message: Message } | { kind: "cancel"; turn: Turn };

export function ConversationWorkspace() {
  const t = useT();
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedTurn, setSelectedTurn] = useState<string | null>(null);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState<unknown>(null);
  const [factsError, setFactsError] = useState<unknown>(null);
  const [actionError, setActionError] = useState<unknown>(null);
  const [factsLoading, setFactsLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [name, setName] = useState("");
  const [newAgent, setNewAgent] = useState("");
  const [reply, setReply] = useState<Message | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [narrow, setNarrow] = useState(false);
  const draftKey = `${selectedId}:${selectedTurn ?? "initial"}`;
  const currentConversation = useRef(selectedId);
  currentConversation.current = selectedId;
  const { detail, messages, connection, error: streamError, refresh: refreshTurn } = useTurn(selectedId, selectedTurn);
  const agent = agents.find(item => item.agent_id === conversation?.bound_agent_id);
  const selectedFact = detail?.turn_id === selectedTurn ? detail : null;

  const refreshList = useCallback(async (signal?: AbortSignal) => {
    try {
      const [items, registered] = await Promise.all([api.conversations(signal), api.agents(signal)]);
      if (signal?.aborted) return;
      setConversations(items); setAgents(registered); setListError(null);
      return items;
    } catch (error) { if (!signal?.aborted) setListError(error); }
    finally { if (!signal?.aborted) setListLoading(false); }
  }, []);

  const refreshFacts = useCallback(async (cid: string, signal?: AbortSignal) => {
    const [conv, items] = await Promise.all([api.conversation(cid, signal), api.turns(cid, signal)]);
    if (signal?.aborted || currentConversation.current !== cid) return;
    setConversation(conv); setTurns(items); setFactsError(null);
    setSelectedTurn(previous => previous && items.some(item => item.turn_id === previous) ? previous : conv.focus_turn_id ?? items[0]?.turn_id ?? null);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    if (window.innerWidth < 1000) { setSidebarOpen(false); setInspectorOpen(false); }
    void refreshList(controller.signal).then(items => {
      if (controller.signal.aborted || !items) return;
      const query = new URLSearchParams(window.location.search);
      const requested = query.get("conversation");
      if (requested && items.some(item => item.conversation_id === requested)) { setSelectedId(requested); setSelectedTurn(query.get("turn")); }
      else if (items.length) setSelectedId(items[0].conversation_id);
    });
    return () => controller.abort();
  }, [refreshList]);

  useEffect(() => {
    const media = window.matchMedia("(max-width: 999px)");
    const update = () => setNarrow(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    if (!narrow || dialog) return;
    const drawer = document.getElementById(sidebarOpen ? "conversation-sidebar" : inspectorOpen && selectedId ? "work-inspector" : "");
    if (!drawer) return;
    const previous = document.activeElement as HTMLElement | null;
    const controls = () => Array.from(drawer.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex="0"]')).filter(element => element.getClientRects().length);
    controls()[0]?.focus();
    const trap = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const items = controls();
      const first = items[0], last = items.at(-1);
      if (!drawer.contains(document.activeElement) || (!event.shiftKey && document.activeElement === last)) { event.preventDefault(); first?.focus(); }
      else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    };
    document.addEventListener("keydown", trap);
    return () => { document.removeEventListener("keydown", trap); if (previous?.isConnected) previous.focus(); };
  }, [narrow, sidebarOpen, inspectorOpen, selectedId, dialog]);

  useEffect(() => {
    if (!selectedId) return;
    const controller = new AbortController();
    setFactsLoading(true); setConversation(null); setTurns([]); setReply(null); setActionError(null);
    void refreshFacts(selectedId, controller.signal).catch(error => { if (!controller.signal.aborted) setFactsError(error); }).finally(() => { if (!controller.signal.aborted) setFactsLoading(false); });
    let refreshing = false;
    const interval = setInterval(async () => {
      if (refreshing) return;
      refreshing = true;
      try { await refreshFacts(selectedId, controller.signal); await refreshList(controller.signal); }
      catch (error) { if (!controller.signal.aborted) setFactsError(error); }
      finally { refreshing = false; }
    }, 4000);
    return () => { controller.abort(); clearInterval(interval); };
  }, [selectedId, refreshFacts, refreshList]);

  useEffect(() => {
    if (!selectedId) return;
    const query = new URLSearchParams({ conversation: selectedId });
    if (selectedTurn) query.set("turn", selectedTurn);
    window.history.replaceState(null, "", `/?${query}`);
    setReply(null);
  }, [selectedId, selectedTurn]);

  useEffect(() => {
    const onEscape = (event: KeyboardEvent) => { if (event.key === "Escape" && window.innerWidth < 1000 && !dialog) { setSidebarOpen(false); setInspectorOpen(false); } };
    document.addEventListener("keydown", onEscape);
    return () => document.removeEventListener("keydown", onEscape);
  }, [dialog]);

  function openDialog(next: Dialog) { setName(""); setNewAgent(agents[0]?.agent_id ?? ""); setActionError(null); setDialog(next); }
  function selectConversation(id: string) { setSelectedId(id); setSelectedTurn(null); if (window.innerWidth < 1000) setSidebarOpen(false); }
  function refresh() { void refreshList(); if (selectedId) void refreshFacts(selectedId).catch(setFactsError); refreshTurn(); }

  async function action(work: () => Promise<void>) {
    if (busy) return false;
    setBusy(true); setActionError(null);
    const cid = selectedId;
    try { await work(); await refreshList(); if (cid && currentConversation.current === cid) await refreshFacts(cid); refreshTurn(); return true; }
    catch (error) { setActionError(error); if (cid && currentConversation.current === cid) await refreshFacts(cid).catch(setFactsError); refreshTurn(); return false; }
    finally { setBusy(false); }
  }

  async function submitDialog() {
    if (!dialog) return;
    const target = dialog;
    await action(async () => {
      if (target.kind === "conversation") {
        const created = await api.createConversation(name.trim() || t("newConversation"), newAgent);
        selectConversation(created.conversation_id);
      } else if (target.kind === "task" && selectedId) {
        const turn = await api.newTask(selectedId, name.trim() || t("task"), selectedFact?.branch_id);
        setSelectedTurn(turn.turn_id);
      } else if (target.kind === "branch" && selectedId) {
        const branch = await api.branch(selectedId, target.message.message_id, name.trim() || t("branchTask"));
        await api.focus(selectedId, branch.initial_turn_id);
        setSelectedTurn(branch.initial_turn_id);
      } else if (target.kind === "cancel") await api.control(target.turn.turn_id, "cancel");
      setDialog(null);
    });
  }

  async function send(body: SendBody) {
    if (!selectedId) return false;
    return action(async () => { const result = await api.send(selectedId, body); setSelectedTurn(result.turn.turn_id); });
  }

  function control(kind: "cancel" | "resume" | "stop-loop") {
    if (!selectedFact) return;
    if (kind === "cancel") openDialog({ kind: "cancel", turn: selectedFact });
    else void action(async () => { await api.control(selectedFact.turn_id, kind); });
  }

  const isBackground = selectedTurn && conversation?.focus_turn_id && selectedTurn !== conversation.focus_turn_id;
  const connectionText = { loading: "syncing", live: "connected", settled: "saved", reconnecting: "reconnecting", disconnected: "disconnected" }[connection];
  const modalTitle = dialog?.kind === "conversation" ? t("newConversation") : dialog?.kind === "task" ? t("newTask") : dialog?.kind === "branch" ? t("createBranch") : t("cancelTitle");

  return <div className={`kane-app-frame workspace ${sidebarOpen ? "sidebar-open" : ""} ${inspectorOpen && selectedId ? "inspector-open" : ""}`}>
    <a className="skip-link" href="#conversation-main">{t("conversations")}</a>
    <aside id="conversation-sidebar" className="kane-sidebar-panel workspace-sidebar" role={narrow ? "dialog" : undefined} aria-modal={narrow && sidebarOpen ? true : undefined} aria-label={t("conversations")} inert={!sidebarOpen}>
      <SidebarNav conversations={conversations} agents={agents} selectedId={selectedId} loading={listLoading} error={listError} onSelect={selectConversation} onCreate={() => openDialog({ kind: "conversation" })} onRefresh={() => void refreshList()} onClose={() => setSidebarOpen(false)} />
    </aside>
    <div className="workspace-body"><TopBar sidebarOpen={sidebarOpen} inspectorOpen={inspectorOpen} online={listLoading ? null : !listError} onSidebar={() => { setSidebarOpen(value => !value); if (window.innerWidth < 1000) setInspectorOpen(false); }} onInspector={() => { setInspectorOpen(value => !value); if (window.innerWidth < 1000) setSidebarOpen(false); }} />
      <div className="workspace-content"><main id="conversation-main" className="conversation-main" tabIndex={-1}>
        {!selectedId ? <div className="workspace-welcome"><BrandMark /><span className="welcome-brand">Kane–Kanaloa</span><h1>{t("welcome")}</h1><p>{t("welcomeText")}</p>{listLoading ? <Loading /> : listError ? <ErrorNotice error={listError} onRetry={() => void refreshList()} /> : agents.length ? <button className="button primary" aria-label={t("start")} onClick={() => openDialog({ kind: "conversation" })}><span aria-hidden="true">＋</span>{t("start")}</button> : <p role="status">{t("noAgents")}</p>}</div> : <>
          <header className="conversation-header"><div className="conversation-heading"><h1>{conversation?.title ?? t("loading")}</h1><div className="conversation-subtitle"><span className={`stream-indicator connection-${connection}`} data-testid="stream-status"><span className="status-dot" />{t(connectionText)}</span>{selectedFact && <><span className="meta-separator">/</span><span className="current-task-name">{turnTitle(selectedFact, t)}</span></>}</div></div><div className="conversation-header-actions"><IconButton label={t("refresh")} onClick={refresh}>↻</IconButton><button className="button secondary compact" onClick={() => openDialog({ kind: "task" })} disabled={busy || !agent}><span aria-hidden="true">＋</span>{t("newTask")}</button></div></header>
          {selectedFact?.branch_id !== "main" && selectedFact && <div className="context-strip"><span aria-hidden="true">⑂</span><strong>{t("branchContext")}</strong><span>{selectedFact.branch_id.slice(-6)}</span><span className="context-description">{t("branchDetail")}</span></div>}
          {isBackground && <div className="context-strip background-context"><span>{t("inspecting")}</span><button className="text-button" onClick={() => void action(async () => { if (selectedId && selectedTurn) await api.focus(selectedId, selectedTurn); })}>{t("focusTask")} →</button></div>}
          {Boolean(factsError) && <ErrorNotice error={factsError} onRetry={refresh} />}
          {Boolean(actionError) && !dialog && <ErrorNotice error={actionError} onRetry={refresh} action />}
          {factsLoading ? <Loading /> : <ChatPanel key={`chat:${draftKey}`} messages={messages} detail={selectedFact} turns={turns} agent={agent} connection={connection} error={streamError} busy={busy} onRefresh={refresh} onBranch={message => openDialog({ kind: "branch", message })} onReply={setReply} onPermission={(requestId, decision) => void action(async () => { if (selectedTurn) await api.permission(selectedTurn, requestId, decision); })} onResume={() => control("resume")} />}
          <Composer key={draftKey} agent={agent} turn={selectedFact} reply={reply} draft={drafts[draftKey] ?? ""} onDraftChange={(value, expected) => setDrafts(previous => expected !== undefined && previous[draftKey] !== expected ? previous : { ...previous, [draftKey]: value })} busy={busy} disabled={!agent || factsLoading || Boolean(factsError)} onSend={send} onClearReply={() => setReply(null)} />
        </>}
      </main>
      {selectedId && <aside id="work-inspector" className="work-inspector" role={narrow ? "dialog" : undefined} aria-modal={narrow && inspectorOpen ? true : undefined} aria-label={t("workPanel")} inert={!inspectorOpen}><WorkInspector turns={turns} detail={selectedFact} agent={agent} focusId={conversation?.focus_turn_id ?? null} selectedId={selectedTurn} busy={busy} onFocus={turn => void action(async () => { if (selectedId) { await api.focus(selectedId, turn.turn_id); setSelectedTurn(turn.turn_id); } })} onInspect={turn => setSelectedTurn(turn.turn_id)} onNewTask={() => openDialog({ kind: "task" })} onControl={control} onClose={() => setInspectorOpen(false)} /></aside>}
      </div>
    </div>
    {(sidebarOpen || (inspectorOpen && selectedId)) && <button className="drawer-backdrop" aria-label={t("close")} onClick={() => { setSidebarOpen(false); setInspectorOpen(false); }} />}
    {dialog && <Modal title={modalTitle} onClose={() => { if (!busy) setDialog(null); }}><form onSubmit={event => { event.preventDefault(); void submitDialog(); }}>
      {dialog.kind === "cancel" ? <p className="modal-description">{t("cancelHint")}</p> : <><label className="form-field">{t(dialog.kind === "conversation" ? "title" : dialog.kind === "task" ? "taskTitle" : "branchName")}<input autoFocus value={name} onChange={event => setName(event.target.value)} placeholder={t(dialog.kind === "conversation" ? "optionalTitle" : dialog.kind === "task" ? "taskPlaceholder" : "branchPlaceholder")} /></label>{dialog.kind === "conversation" && <label className="form-field">{t("agent")}<select aria-label={t("agent")} value={newAgent} onChange={event => setNewAgent(event.target.value)}>{agents.map(item => <option value={item.agent_id} key={item.agent_id}>{item.agent_id === "kanaloa" ? "Kanaloa" : item.agent_id} · {item.status === "ready" ? t("agentReady") : item.status === "idle" ? t("agentIdle") : item.status}</option>)}</select></label>}{dialog.kind === "branch" && <><blockquote className="branch-preview">{dialog.message.content}</blockquote><p className="modal-description">{t("branchHint")}</p></>}{dialog.kind === "task" && <p className="modal-description">{t("newTaskHint")}{agent && !agent.supports_parallel_sessions && ` ${t("parallelUnavailable")}`}</p>}</>}
      {Boolean(actionError) && <ErrorNotice error={actionError} action />}
      <footer className="modal-actions"><button type="button" className="button secondary" onClick={() => setDialog(null)} disabled={busy}>{t(dialog.kind === "cancel" ? "keepWorking" : "cancel")}</button><button type="submit" className={`button ${dialog.kind === "cancel" ? "danger" : "primary"}`} disabled={busy || (dialog.kind === "conversation" && !newAgent)}>{busy ? t("loading") : t(dialog.kind === "conversation" ? "create" : dialog.kind === "task" ? "createTask" : dialog.kind === "branch" ? "createBranch" : "confirmCancel")}</button></footer>
    </form></Modal>}
  </div>;
}
