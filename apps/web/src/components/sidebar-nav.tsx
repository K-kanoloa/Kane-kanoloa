"use client";

import { ArrowLeft, MoreHorizontal, Pin, Plus, Search } from "lucide-react";
import { type Agent, type Conversation } from "@/lib/api";
import { useEffect, useState } from "react";
import { useT } from "@/lib/i18n/LocaleProvider";
import { ErrorNotice, IconButton, Loading, Time } from "./workspace/ui";

export function SidebarNav({ mode, conversations, agents, selectedAgentId, selectedId, loading, error, onSelect, onCreate, onRefresh, onClose, onRename, onDelete }: { mode: "agent" | "recent"; conversations: Conversation[]; agents: Agent[]; selectedAgentId: string | null; selectedId: string | null; loading: boolean; error: unknown; onSelect: (id: string) => void; onCreate: () => void; onRefresh: () => void; onClose: () => void; onRename: (conversation: Conversation) => void; onDelete: (conversation: Conversation) => void }) {
  const t = useT();
  const [query, setQuery] = useState("");
  const [pins, setPins] = useState<string[]>([]);
  const [menu, setMenu] = useState<string | null>(null);
  const [preferenceError, setPreferenceError] = useState<unknown>(null);
  useEffect(() => {
    try { const stored: unknown = JSON.parse(localStorage.getItem("kane.conversation-pins") || "[]"); if (Array.isArray(stored)) setPins(stored.filter((id): id is string => typeof id === "string")); } catch { /* Optional device preference. */ }
  }, []);
  useEffect(() => {
    if (!menu) return;
    document.querySelector<HTMLButtonElement>(`#menu-${menu} + .conversation-menu button`)?.focus();
    const close = (event: PointerEvent) => { if (!(event.target as Element).closest(".conversation-menu, .conversation-menu-toggle")) setMenu(null); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") { document.getElementById(`menu-${menu}`)?.focus(); setMenu(null); } };
    document.addEventListener("pointerdown", close); document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", close); document.removeEventListener("keydown", escape); };
  }, [menu]);
  function togglePin(id: string) {
    const next = pins.includes(id) ? pins.filter(item => item !== id) : [...pins, id];
    try { localStorage.setItem("kane.conversation-pins", JSON.stringify(next)); setPins(next); setMenu(null); setPreferenceError(null); } catch (error) { setPreferenceError(error); }
  }
  const filtered = conversations.filter(item => item.title.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  const currentAgent = agents.find(item => item.agent_id === selectedAgentId);
  const heading = mode === "recent" ? t("recentConversations") : currentAgent?.display_name || currentAgent?.agent_id || t("agents");
  return <>
    <div className="branch-head"><IconButton label={t("openAgents")} className="drawer-close" onClick={onClose}><ArrowLeft size={17} /></IconButton><div className="branch-head-copy"><div className="branch-agent">{heading}</div><div className="branch-caption">{mode === "recent" ? t("recentShort") : t("allConversations")}</div></div><IconButton label={t("newConversation")} onClick={onCreate} disabled={!agents.some(agent => agent.status !== "unavailable")}><Plus size={19} /></IconButton></div>
    <label className="conversation-search agent-search"><Search size={15} /><input type="search" aria-label={t("search")} placeholder={t("search")} value={query} onChange={event => setQuery(event.target.value)} /></label>
    <div className="section-label"><span>{mode === "recent" ? t("recentConversations") : t("conversations")}</span><span>{filtered.length}</span></div>
    <nav className="conversation-list" aria-label={mode === "recent" ? t("recentConversations") : t("conversations")}>
      {Boolean(preferenceError) && <ErrorNotice error={preferenceError} />}
      {loading ? <Loading /> : error ? <ErrorNotice error={error} onRetry={onRefresh} /> : filtered.length ? [...filtered].sort((a, b) => Number(pins.includes(b.conversation_id)) - Number(pins.includes(a.conversation_id)) || Date.parse(b.updated_at) - Date.parse(a.updated_at)).map(item => {
        const owner = agents.find(agent => agent.agent_id === item.bound_agent_id);
        return <div className="conversation-item" key={item.conversation_id}><button className={`conversation-row ${item.conversation_id === selectedId ? "selected" : ""}`} aria-current={item.conversation_id === selectedId ? "page" : undefined} onClick={() => onSelect(item.conversation_id)}>
          <span className="conversation-row-top"><strong title={item.title}>{pins.includes(item.conversation_id) && <Pin className="pin-marker" size={11} />}{item.title}</strong><Time value={item.updated_at} calendar /></span>
          <small>{pins.includes(item.conversation_id) && `${t("pinned")} · `}{mode === "recent" ? owner?.display_name || owner?.agent_id || item.bound_agent_id : item.focus_turn_id ? t("focus") : t("noMessages")}</small>
        </button><IconButton id={`menu-${item.conversation_id}`} className="conversation-menu-toggle" label={`${t("conversationActions")}: ${item.title}`} aria-expanded={menu === item.conversation_id} onClick={() => setMenu(menu === item.conversation_id ? null : item.conversation_id)}><MoreHorizontal size={18} /></IconButton>{menu === item.conversation_id && <div className="conversation-menu" role="menu"><button role="menuitem" onClick={() => togglePin(item.conversation_id)}>{t(pins.includes(item.conversation_id) ? "unpinConversation" : "pinConversation")}</button><button role="menuitem" onClick={() => { setMenu(null); onRename(item); }}>{t("renameConversation")}</button><button role="menuitem" onClick={() => { setMenu(null); onDelete(item); }}>{t("deleteConversation")}</button></div>}</div>;
      }) : <p className="sidebar-empty">{t(query ? "noMatches" : "noConversations")}</p>}
    </nav>
  </>;
}
