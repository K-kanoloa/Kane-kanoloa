"use client";

import { useEffect, useRef, useState } from "react";
import { type Agent, type Decision, type Message, type Turn, type TurnDetail } from "@/lib/api";
import { useT } from "@/lib/i18n/LocaleProvider";
import { type Connection } from "@/lib/use-turn";
import { activityLabel, BrandMark, ErrorNotice, IconButton, Loading, Time, turnTitle } from "./ui";

export function ChatPanel({ messages, detail, turns, agent, connection, error, busy, onRefresh, onBranch, onReply, onPermission, onResume }: { messages: Message[]; detail: TurnDetail | null; turns: Turn[]; agent?: Agent; connection: Connection; error: unknown; busy: boolean; onRefresh: () => void; onBranch: (message: Message) => void; onReply: (message: Message) => void; onPermission: (requestId: string, decision: Decision) => void; onResume: () => void }) {
  const t = useT();
  const scroll = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  const [copied, setCopied] = useState<string | null>(null);
  const [copyError, setCopyError] = useState(false);
  useEffect(() => { if (following.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight; }, [messages, detail?.partial_output, detail?.pending_permissions.length]);
  const latestChanges = new Map<string, Message>();
  for (const message of messages) if (message.target_message_id && message.kind !== "normal") latestChanges.set(message.target_message_id, message);
  const visibleMessages = messages.filter(message => message.kind === "normal" || !messages.some(original => original.message_id === message.target_message_id));
  const latestActivity = detail?.events.at(-1);
  async function copy(message: Message, content: string) {
    try { await navigator.clipboard.writeText(content); setCopied(message.message_id); setCopyError(false); }
    catch { setCopyError(true); }
  }

  return <div className="chat-scroll-wrap"><div ref={scroll} className="chat-scroll" onScroll={() => { const element = scroll.current!; following.current = element.scrollHeight - element.scrollTop - element.clientHeight < 90; setAtBottom(following.current); }}>
    <div className="transcript" aria-label={t("conversations")}>
      {connection === "loading" && !detail ? <Loading /> : null}
      {Boolean(error) && <ErrorNotice error={error} onRetry={onRefresh} />}
      {!messages.length && !detail?.partial_output && connection !== "loading" && !error && <div className="chat-empty"><BrandMark /><h2>{t("noHistory")}</h2><p>{t("noHistoryText")}</p></div>}
      {visibleMessages.map(message => {
        const change = latestChanges.get(message.message_id);
        const retracted = change?.kind === "retract" || message.kind === "retract";
        const content = change?.kind === "edit" ? change.content : message.content;
        const sourceTurn = turns.find(turn => turn.turn_id === message.turn_id);
        const sender = message.sender === "user" ? t("you") : message.sender === "system" ? t("system") : message.sender_id === "kanaloa" || !message.sender_id ? "Kanaloa" : message.sender_id;
        return <article className={`message-row message-${message.sender}`} key={message.message_id} data-message-id={message.message_id} data-testid={`message-${message.sender}`}>
          <div className="message-avatar" aria-hidden="true">{message.sender === "agent" ? "K" : message.sender === "user" ? "Y" : "·"}</div>
          <div className="message-main"><header className="message-meta"><strong>{sender}</strong><Time value={message.created_at} />{sourceTurn && sourceTurn.turn_id !== detail?.turn_id && <span className="message-source" title={sourceTurn.title ?? undefined}>{turnTitle(sourceTurn, t)}</span>}</header>
            <div className={`message-content ${retracted ? "retracted" : ""}`}>{retracted ? t("retracted") : content}</div>
            {(change?.kind === "edit" || message.kind === "edit") && <small className="edited-note">{t("edited")}</small>}
            <div className="message-actions">{!retracted && <><IconButton label={copied === message.message_id ? t("copied") : t("copy")} onClick={() => void copy(message, content)}>{copied === message.message_id ? "✓" : "⧉"}</IconButton><IconButton label={t("reply")} onClick={() => onReply(message)}>↩</IconButton></>}{agent?.branch_mode !== "unsupported" && agent && <button className="text-button branch-action" onClick={() => onBranch(message)}><span aria-hidden="true">⑂</span>{t("branchFrom")}</button>}</div>
          </div>
        </article>;
      })}
      {copyError && <p className="inline-error" role="alert">{t("copyFailed")}</p>}
      {detail?.partial_output && <article className="message-row message-agent" data-testid="partial-reply"><div className="message-avatar" aria-hidden="true">K</div><div className="message-main"><header className="message-meta"><strong>{detail.bound_agent_id === "kanaloa" ? "Kanaloa" : detail.bound_agent_id}</strong><span>{t(detail.status === "interrupted" || detail.status === "failed" ? "partial" : "streaming")}</span></header><div className="message-content">{detail.partial_output}</div>{detail.status === "running" && <span className="stream-caret" aria-hidden="true" />}</div></article>}
      {detail?.status === "running" && !detail.partial_output && <div className="working-indicator" role="status"><span className="working-dots"><i /><i /><i /></span>{latestActivity ? activityLabel(latestActivity, t) : t("eventProgress")}</div>}
      {detail && ["interrupted", "failed"].includes(detail.status) && <div className={`runtime-notice ${detail.status}`} role="status"><div><strong>{t(detail.status === "failed" ? "failure" : "interruption")}</strong>{detail.interrupt_reason && <p>{detail.interrupt_reason}</p>}{detail.status === "interrupted" && !agent?.supports_resume && <p>{t("unavailableResume")}</p>}</div>{detail.status === "interrupted" && agent?.supports_resume && <button className="button secondary compact" onClick={onResume} disabled={busy}>{t("resume")}</button>}</div>}
      {detail?.status === "waiting_user" && detail.pending_permissions.length === 0 && <div className="runtime-notice waiting_user"><strong>{t("waiting")}</strong><p>{t("permissionMissing")}</p></div>}
      {detail?.pending_permissions.map(permission => <section className="approval-card" key={permission.request_id} aria-label={t("approval")}><div className="approval-symbol" aria-hidden="true">?</div><div><h3>{t("approval")}</h3><p>{permission.title}</p><div className="approval-actions"><button className="button primary compact" onClick={() => onPermission(permission.request_id, "allow-once")} disabled={busy}>{t("allow")}</button><button className="button secondary compact" onClick={() => onPermission(permission.request_id, "reject-once")} disabled={busy}>{t("deny")}</button><button className="text-button" onClick={() => onPermission(permission.request_id, "cancelled")} disabled={busy}>{t("cancelRequest")}</button></div></div></section>)}
    </div>
  </div>{!atBottom && <button className="jump-latest button secondary compact" onClick={() => { following.current = true; if (scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight; setAtBottom(true); }}>↓ {t("scrollLatest")}</button>}</div>;
}
