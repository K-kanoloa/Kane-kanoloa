"use client";

import { useEffect, useRef, useState } from "react";
import { type Agent, type Message, type SendBody, type TurnDetail } from "@/lib/api";
import { useT } from "@/lib/i18n/LocaleProvider";
import { IconButton } from "./ui";

export function Composer({ agent, turn, reply, draft, onDraftChange, busy, disabled, onSend, onClearReply }: { agent?: Agent; turn: TurnDetail | null; reply: Message | null; draft: string; onDraftChange: (value: string, expected?: string) => void; busy: boolean; disabled: boolean; onSend: (body: SendBody) => Promise<boolean>; onClearReply: () => void }) {
  const t = useT();
  const [advanced, setAdvanced] = useState(false);
  const [loop, setLoop] = useState(false);
  const [limit, setLimit] = useState("default");
  const [custom, setCustom] = useState("5");
  const input = useRef<HTMLTextAreaElement>(null);
  const canConfigureLoop = agent?.agent_id === "kanaloa" && (!turn || ((turn.status !== "running" || !turn.native_session_ref) && turn.status !== "waiting_user"));
  const validLimit = limit !== "custom" || (/^[1-9]\d*$/.test(custom) && Number.isSafeInteger(Number(custom)));
  const running = turn?.status === "running" && Boolean(turn.native_session_ref);
  useEffect(() => { const element = input.current; if (element) { element.style.height = "auto"; element.style.height = `${Math.min(element.scrollHeight, 192)}px`; } }, [draft]);

  async function send() {
    if (!draft.trim() || disabled || busy || (loop && canConfigureLoop && !validLimit)) return;
    const body: SendBody = { content: draft, ...(turn ? { turn_id: turn.turn_id } : {}), ...(reply ? { reply_to_message_id: reply.message_id } : {}) };
    if (loop && canConfigureLoop) { body.loop_mode = true; body.max_iterations = limit === "unlimited" ? null : limit === "custom" ? Number(custom) : 5; }
    if (await onSend(body)) { onDraftChange("", body.content); setLoop(false); onClearReply(); input.current?.focus(); }
  }

  return <div className="composer-region">
    {running && agent?.steer_mode !== "native" && <p className="delivery-note">{t(agent?.steer_mode === "safe_boundary" ? "queuedSafe" : "queuedFollowup")}</p>}
    <form className="composer" onSubmit={event => { event.preventDefault(); void send(); }}>
      {reply && <div className="reply-preview"><span><small>{t("replying")}</small>{reply.content}</span><IconButton label={t("clearReply")} onClick={onClearReply}>×</IconButton></div>}
      <textarea ref={input} aria-label={t("message")} placeholder={t(running ? "adjustPlaceholder" : "messagePlaceholder")} rows={2} value={draft} disabled={disabled} onChange={event => onDraftChange(event.target.value)} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); } }} />
      {advanced && agent?.agent_id === "kanaloa" && <div className="loop-settings"><label className="check-label"><input type="checkbox" checked={loop} disabled={!canConfigureLoop} onChange={event => setLoop(event.target.checked)} />{t("loopEnable")}</label>{!canConfigureLoop ? <p>{t("loopUnavailable")}</p> : loop && <div className="loop-fields"><select aria-label={t("loopLimit")} value={limit} onChange={event => setLimit(event.target.value)}><option value="default">{t("loopDefault")}</option><option value="unlimited">{t("loopUnlimited")}</option><option value="custom">{t("loopCustom")}</option></select>{limit === "custom" && <label>{t("iterations")}<input type="number" min="1" step="1" aria-label={t("iterations")} value={custom} onChange={event => setCustom(event.target.value)} aria-invalid={!validLimit} /></label>}{!validLimit && <span role="alert">{t("invalidIterations")}</span>}</div>}</div>}
      <div className="composer-toolbar"><div className="composer-agent"><span className="agent-initial">K</span>{agent?.agent_id === "kanaloa" ? "Kanaloa" : agent?.agent_id ?? t("agentUnavailable")}{agent?.agent_id === "kanaloa" && <button type="button" className={`loop-toggle ${loop && canConfigureLoop ? "enabled" : ""}`} aria-label={t("loopSettings")} aria-expanded={advanced} onClick={() => setAdvanced(value => !value)}><span aria-hidden="true">↻</span>{loop && canConfigureLoop ? t("loop") : t("normal")}<span aria-hidden="true">⌄</span></button>}</div><button type="submit" className="send-button" title={t("send")} aria-label={t("send")} disabled={disabled || busy || !draft.trim() || (loop && canConfigureLoop && !validLimit)}>{busy ? <span className="spinner" /> : <span aria-hidden="true">↑</span>}</button></div>
    </form>
  </div>;
}
