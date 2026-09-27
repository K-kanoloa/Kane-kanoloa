"use client";

import { useState } from "react";
import { type Agent, type Conversation } from "@/lib/api";
import { useT } from "@/lib/i18n/LocaleProvider";
import { BrandMark, ErrorNotice, IconButton, Loading } from "./workspace/ui";

export function SidebarNav({ conversations, agents, selectedId, loading, error, onSelect, onCreate, onRefresh, onClose }: { conversations: Conversation[]; agents: Agent[]; selectedId: string | null; loading: boolean; error: unknown; onSelect: (id: string) => void; onCreate: () => void; onRefresh: () => void; onClose: () => void }) {
  const t = useT();
  const [query, setQuery] = useState("");
  const filtered = conversations.filter(item => item.title.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  return <>
    <div className="sidebar-brand"><BrandMark /><div><strong>Kane</strong><span>{t("workspace")}</span></div><IconButton label={t("close")} className="drawer-close" onClick={onClose}>×</IconButton></div>
    <div className="sidebar-tools"><button className="button primary new-conversation" onClick={onCreate} disabled={!agents.length}><span aria-hidden="true">＋</span>{t("newConversation")}</button><label className="search-field"><span aria-hidden="true">⌕</span><input type="search" placeholder={t("search")} aria-label={t("search")} value={query} onChange={event => setQuery(event.target.value)} /></label></div>
    <div className="section-label"><span>{t("conversations")}</span><span>{conversations.length}</span></div>
    <nav className="conversation-list" aria-label={t("conversations")}>{loading ? <Loading /> : error ? <ErrorNotice error={error} onRetry={onRefresh} /> : filtered.length ? filtered.map(item => <button key={item.conversation_id} className={`conversation-row ${item.conversation_id === selectedId ? "selected" : ""}`} aria-current={item.conversation_id === selectedId ? "page" : undefined} onClick={() => onSelect(item.conversation_id)}><span className="conversation-symbol" aria-hidden="true">◌</span><span><strong>{item.title}</strong><small>{item.bound_agent_id === "kanaloa" ? "Kanaloa" : item.bound_agent_id}</small></span>{item.focus_turn_id && <span className="focus-mark" title={t("focus")} aria-label={t("focus")} />}</button>) : <p className="sidebar-empty">{t(query ? "noMatches" : "noConversations")}</p>}</nav>
    <footer className="sidebar-footer"><span className="local-symbol" aria-hidden="true">⌂</span><div><strong>{t("local")}</strong><small>Kane–Kanaloa</small></div><IconButton label={t("refresh")} onClick={onRefresh}>↻</IconButton></footer>
  </>;
}
