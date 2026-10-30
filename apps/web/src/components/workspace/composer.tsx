"use client";

import { ArrowUp, ChevronDown, Plus, RotateCw, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { type Agent, type Message, type SendBody, type TurnDetail } from "@/lib/api";
import { useT } from "@/lib/i18n/LocaleProvider";
import { AgentAvatar, IconButton } from "./ui";

export type LoopDraft = { enabled: boolean; limit: "default" | "unlimited" | "custom"; custom: string };
export const defaultLoopDraft: LoopDraft = { enabled: false, limit: "default", custom: "5" };

export function Composer({ onNewTask, agent, turn, reply, draft, loopDraft, onLoopChange, onControl, onSettings, onDraftChange, busy, disabled, onSend, onClearReply }: { onNewTask: () => void; agent?: Agent; turn: TurnDetail | null; reply: Message | null; draft: string; loopDraft: LoopDraft; onLoopChange: (value: LoopDraft) => void; onControl: (action: "cancel" | "resume" | "stop-loop") => void; onSettings: () => void; onDraftChange: (value: string, expected?: string) => void; busy: boolean; disabled: boolean; onSend: (body: SendBody) => Promise<boolean>; onClearReply: () => void }) {
  const t = useT();
  const [advanced, setAdvanced] = useState(false);
  const { enabled: loop, limit, custom } = loopDraft;
  const input = useRef<HTMLTextAreaElement>(null);
  const externalAgent = Boolean(agent && agent.agent_id !== "kanaloa");
  const canConfigureLoop = externalAgent || (agent?.agent_id === "kanaloa" && (!turn || ((turn.status !== "running" || !turn.native_session_ref) && turn.status !== "waiting_user")));
  const validLimit = limit !== "custom" || (/^[1-9]\d*$/.test(custom) && Number.isSafeInteger(Number(custom)));
  const running = turn?.status === "running" && Boolean(turn.native_session_ref);
  useEffect(() => { const element = input.current; if (element) { element.style.height = "auto"; element.style.height = `${Math.min(element.scrollHeight, 192)}px`; } }, [draft]);

  async function send() {
    if (!draft.trim() || disabled || busy || (loop && canConfigureLoop && !validLimit)) return;
    const body: SendBody = { content: draft, ...(turn ? { turn_id: turn.turn_id } : {}), ...(reply ? { reply_to_message_id: reply.message_id } : {}) };
    if (loop && canConfigureLoop) {
      if (externalAgent) {
        const instruction = limit === "unlimited" ? t("externalLoopUnlimitedRequest") : t("externalLoopRequest").replace("{iterations}", limit === "custom" ? custom : "5");
        body.content = `${draft}\n\n${instruction}`;
      } else {
        body.loop_mode = true;
        body.max_iterations = limit === "unlimited" ? null : limit === "custom" ? Number(custom) : 5;
      }
    }
    if (await onSend(body)) { onDraftChange("", draft); onLoopChange({ ...loopDraft, enabled: false }); onClearReply(); input.current?.focus(); }
  }

  return <div className="composer-region">
    <div className="composer-task-actions"><button type="button" className="text-button" disabled={busy || disabled} onClick={onNewTask}><Plus size={14} aria-hidden="true" />{t("newTask")}</button></div>
    {turn && <div className="composer-controls"><span>{turn.loop ? `${t("loopRunning")} · ${turn.loop.current_iteration} / ${turn.loop.max_iterations ?? "∞"}` : t(`status.${turn.status}`)}</span>{turn.loop && <button type="button" className="text-button" disabled={busy || turn.loop.stop_requested} onClick={() => onControl("stop-loop")}>{t(turn.loop.stop_requested ? "stopRequested" : "stopLoop")}</button>}{(turn.status === "running" || turn.status === "waiting_user") && agent?.supports_cancel && <button type="button" className="text-button" disabled={busy} onClick={() => onControl("cancel")}>{t("cancelExecution")}</button>}{turn.status === "interrupted" && agent?.supports_resume && <button type="button" className="text-button" disabled={busy} onClick={() => onControl("resume")}>{t("resume")}</button>}</div>}
    {running && agent?.steer_mode !== "native" && <p className="delivery-note">{t(agent?.steer_mode === "safe_boundary" ? "queuedSafe" : "queuedFollowup")}</p>}
    <form className="composer" onSubmit={event => { event.preventDefault(); void send(); }}>
      {reply && <div className="reply-preview"><span><small>{t("replying")}</small>{reply.content}</span><IconButton label={t("clearReply")} onClick={onClearReply}><X size={16} /></IconButton></div>}
      <textarea ref={input} aria-label={t("message")} placeholder={t(running ? "adjustPlaceholder" : "messagePlaceholder")} rows={2} value={draft} disabled={disabled} onChange={event => onDraftChange(event.target.value)} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); } }} />
      {advanced && agent && <div className="loop-settings"><label className="check-label"><input type="checkbox" checked={loop} disabled={!canConfigureLoop || busy} onChange={event => onLoopChange({ ...loopDraft, enabled: event.target.checked })} />{t("loopEnable")}</label>{!canConfigureLoop ? <p>{t("loopUnavailable")}</p> : loop && <><div className="loop-fields"><select aria-label={t("loopLimit")} disabled={busy} value={limit} onChange={event => onLoopChange({ ...loopDraft, limit: event.target.value as LoopDraft["limit"] })}><option value="default">{t("loopDefault")}</option><option value="unlimited">{t("loopUnlimited")}</option><option value="custom">{t("loopCustom")}</option></select>{limit === "custom" && <label>{t("iterations")}<input type="number" min="1" step="1" disabled={busy} aria-label={t("iterations")} value={custom} onChange={event => onLoopChange({ ...loopDraft, custom: event.target.value })} aria-invalid={!validLimit} /></label>}{!validLimit && <span role="alert">{t("invalidIterations")}</span>}</div>{externalAgent ? <p>{limit === "unlimited" ? t("externalLoopUnlimitedRequest") : t("externalLoopRequest").replace("{iterations}", limit === "custom" ? custom : "5")}</p> : <p>{t(limit === "unlimited" ? "loopUnlimitedNote" : "loopDraftNote")}</p>}</>}</div>}
      <div className="composer-toolbar"><div className="composer-agent"><button type="button" className="composer-agent-button" title={t("agentSettings")} onClick={onSettings}><AgentAvatar agentId={agent?.agent_id || ""} name={agent?.display_name || undefined} />{agent?.display_name || agent?.agent_id || t("agentUnavailable")}</button>{agent && <button type="button" className={`loop-toggle ${turn?.loop || (loop && canConfigureLoop) ? "enabled" : ""}`} aria-label={t("loopSettings")} aria-expanded={advanced} onClick={() => setAdvanced(value => !value)}><RotateCw size={13} aria-hidden="true" />{t(externalAgent ? "externalLoopLabel" : "loop")}{!turn?.loop && <span>{loop && canConfigureLoop ? "ON" : "OFF"}</span>}<ChevronDown size={12} aria-hidden="true" /></button>}</div><button type="submit" className="send-button" title={t("send")} aria-label={t("send")} disabled={disabled || busy || !draft.trim() || (loop && canConfigureLoop && !validLimit)}>{busy ? <span className="spinner" /> : <ArrowUp size={20} aria-hidden="true" />}</button></div>
    </form>
  </div>;
}
