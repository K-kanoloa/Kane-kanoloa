"use client";

import { useEffect, useRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { ApiRequestError, type Activity, type Turn, type TurnStatus } from "@/lib/api";
import { useLocale, useT } from "@/lib/i18n/LocaleProvider";

export function IconButton({ label, children, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string; children: ReactNode }) {
  return <button type="button" {...props} className={`icon-button ${props.className ?? ""}`} aria-label={label} title={label}><span aria-hidden="true">{children}</span></button>;
}

export function BrandMark({ small = false }: { small?: boolean }) {
  return <span className={`brand-mark ${small ? "small" : ""}`}><img src="/kane-octopus.png" alt="Kane" width={40} height={40} /></span>;
}

export function Status({ status }: { status: TurnStatus }) {
  const t = useT();
  return <span className={`turn-status status-${status}`} data-status={status}><span className="status-dot" />{t(`status.${status}`)}</span>;
}

export function turnTitle(turn: Turn, t: (key: string) => string) {
  if (turn.title === "Initial Turn") return t("initialTurn");
  if (!turn.title || turn.title === "New Turn") return t("task");
  if (turn.title.startsWith("Branch from msg_")) return t("branchTask");
  return turn.title;
}

export function Time({ value, full = false }: { value: string; full?: boolean }) {
  const { locale } = useLocale();
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return <time dateTime={value} title={date.toLocaleString(locale)}>{full ? date.toLocaleString(locale, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : date.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" })}</time>;
}

export function ErrorNotice({ error, onRetry, action = false }: { error: unknown; onRetry?: () => void; action?: boolean }) {
  const t = useT();
  const auth = error instanceof ApiRequestError && [401, 403].includes(error.status);
  const unavailable = error instanceof TypeError || (error instanceof ApiRequestError && error.status === 502);
  return <div className="error-notice" role="alert"><div><strong>{t(auth ? "accessDenied" : unavailable ? "unavailable" : "error")}</strong><p>{auth ? t("accessHint") : unavailable ? t("unavailableHint") : error instanceof Error ? error.message : String(error)}</p>{action && <p>{t("noActionRetry")}</p>}</div>{onRetry && <button className="button secondary compact" onClick={onRetry}>{t("refresh")}</button>}</div>;
}

export function Loading({ label }: { label?: string }) {
  const t = useT();
  return <div className="loading-state" role="status"><span className="spinner" />{label ?? t("loading")}</div>;
}

export function activityLabel(event: Activity, t: (key: string) => string) {
  if (event.event_type === "status_change") return event.payload.status ? t(`status.${event.payload.status}`) : t(event.payload.boundary ? "eventBoundary" : "eventStatus");
  const keys: Record<string, string> = { thinking: "eventThinking", reading: "eventReading", progress: "eventProgress", tool_start: "eventToolStart", tool_end: "eventToolEnd" };
  return `${t(keys[event.event_type] ?? "eventProgress")}${event.payload.tool ? ` · ${event.payload.tool}` : ""}`;
}

export function Modal({ title, children, onClose }: { title: string; children: ReactNode; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const t = useT();
  useEffect(() => {
    const dialog = ref.current!;
    const previous = document.activeElement as HTMLElement | null;
    dialog.showModal();
    return () => { dialog.close(); previous?.focus(); };
  }, []);
  return <dialog className="modal" ref={ref} aria-label={title} onCancel={event => { event.preventDefault(); onClose(); }} onClick={event => { if (event.target === ref.current) { const rect = ref.current.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onClose(); } }}><header><h2>{title}</h2><IconButton label={t("close")} onClick={onClose}>×</IconButton></header>{children}</dialog>;
}
