"use client";

import { useEffect, useState } from "react";
import type { Agent } from "@/lib/api";
import { useT } from "@/lib/i18n/LocaleProvider";
import { ErrorNotice, IconButton, Loading } from "./workspace/ui";

export function AgentDirectory({ agents, selectedId, loading, error, onSelect, onRename, onDisconnect, onCreateBuiltin, onAddExternal, onRefresh, onClose }: {
  agents: Agent[];
  selectedId: string | null;
  loading: boolean;
  error: unknown;
  onSelect: (agentId: string) => void;
  onRename: (agentId: string) => void;
  onDisconnect: (agentId: string) => void;
  onCreateBuiltin: () => void;
  onAddExternal: () => void;
  onRefresh: () => void;
  onClose: () => void;
}) {
  const t = useT();
  const [query, setQuery] = useState("");
  const [pins, setPins] = useState<string[]>([]);
  const [menu, setMenu] = useState<string | null>(null);
  const [pinError, setPinError] = useState<unknown>(null);
  useEffect(() => { try { const saved: unknown = JSON.parse(localStorage.getItem("kane.agent-pins") || "[]"); if (Array.isArray(saved)) setPins(saved.filter((id): id is string => typeof id === "string")); } catch { /* Device-only preference. */ } }, []);
  useEffect(() => {
    if (!menu) return;
    const close = (event: PointerEvent) => { if (!(event.target as Element).closest(".agent-menu, .agent-rename")) setMenu(null); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setMenu(null); };
    document.addEventListener("pointerdown", close); document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", escape); };
  }, [menu]);
  const filtered = agents.filter(agent => (agent.display_name || agent.agent_id).toLocaleLowerCase().includes(query.toLocaleLowerCase())).sort((a, b) => Number(pins.includes(b.agent_id)) - Number(pins.includes(a.agent_id)));
  function togglePin(id: string) { const next = pins.includes(id) ? pins.filter(item => item !== id) : [...pins, id]; try { localStorage.setItem("kane.agent-pins", JSON.stringify(next)); setPins(next); setPinError(null); setMenu(null); } catch (error) { setPinError(error); } }
  const builtin = filtered.filter(agent => agent.agent_id === "kanaloa");
  const external = filtered.filter(agent => agent.agent_id !== "kanaloa");
  const renderAgent = (agent: Agent) => {
    const name = agent.display_name || agent.agent_id;
    return <div key={agent.agent_id} className={`agent-row ${agent.agent_id === selectedId ? "selected" : ""}`}>
      <button type="button" className="agent-row-select" aria-pressed={agent.agent_id === selectedId} onClick={() => onSelect(agent.agent_id)}>
        <span className={`agent-avatar ${agent.agent_id === "kanaloa" ? "builtin" : ""}`} aria-hidden="true">{name.trim().slice(0, 2).toUpperCase()}<i className={`agent-status-dot ${agent.status}`} /></span>
        <span className="agent-row-copy"><strong>{name}</strong><small>{agent.agent_id === "kanaloa" ? "Kanaloa" : agent.agent_id}</small></span>
      </button>
      <IconButton className="agent-rename" label={`${t("agentActions")} · ${name}`} aria-expanded={menu === agent.agent_id} onClick={() => setMenu(menu === agent.agent_id ? null : agent.agent_id)}>…</IconButton>
      {menu === agent.agent_id && <div className="conversation-menu agent-menu" role="menu"><button role="menuitem" onClick={() => togglePin(agent.agent_id)}>{t(pins.includes(agent.agent_id) ? "unpinConversation" : "pinConversation")}</button><button role="menuitem" onClick={() => { setMenu(null); onRename(agent.agent_id); }}>{t("agentSettings")}</button>{agent.agent_id !== "kanaloa" && <button role="menuitem" onClick={() => { setMenu(null); onDisconnect(agent.agent_id); }}>{t("disconnectAgent")}</button>}</div>}
    </div>;
  };
  return <>
    <header className="agent-directory-brand"><div><strong>Kane</strong><span>{agents.filter(agent => agent.agent_id === "kanaloa").length} Kanaloa · {agents.filter(agent => agent.agent_id !== "kanaloa" && agent.status === "ready").length} {t("externalOnline")}</span></div><IconButton label={t("refresh")} onClick={onRefresh}>↻</IconButton><IconButton className="directory-add" label={t("connectAgent")} onClick={onAddExternal}>+</IconButton><IconButton label={t("close")} className="drawer-close" onClick={onClose}>×</IconButton></header>
    <label className="agent-search"><span aria-hidden="true">⌕</span><input type="search" aria-label={t("searchAgents")} placeholder={t("searchAgents")} value={query} onChange={event => setQuery(event.target.value)} /></label>
    <div className="agent-directory-label section-label"><span>{t("selectAgent")}</span></div>
    <nav className="agent-list" aria-label={t("agents")}>
      {Boolean(pinError) && <ErrorNotice error={pinError} />}
      {loading && !agents.length ? <Loading /> : error && !agents.length ? <ErrorNotice error={error} onRetry={onRefresh} /> : filtered.length ? <>
        {builtin.length > 0 && <section className="agent-group"><div className="agent-group-title"><span>Kanaloa</span><IconButton label={t("newConversation")} onClick={onCreateBuiltin}>+</IconButton></div>{builtin.map(renderAgent)}</section>}
        <section className="agent-group"><div className="agent-group-title"><span>External Agents</span></div>{external.map(renderAgent)}</section>
      </> : <p className="sidebar-empty">{t(query ? "noMatches" : "noAgents")}</p>}
    </nav>
  </>;
}
