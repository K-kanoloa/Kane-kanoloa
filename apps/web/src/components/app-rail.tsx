"use client";

import { useT } from "@/lib/i18n/LocaleProvider";
import { IconButton } from "./workspace/ui";

export function AppRail({ online, onChat, onRecents, onKanaloaSettings, onSettings }: {
  online: boolean | null;
  onChat: () => void;
  onRecents: () => void;
  onKanaloaSettings: () => void;
  onSettings: () => void;
}) {
  const t = useT();
  return <nav className="app-rail" aria-label={t("primaryNavigation")}>
    <div className="app-rail-brand"><img className="app-rail-logo" src="/kane-octopus.png" alt="Kane" width={56} height={56} /></div>
    <div className="app-rail-nav">
      <span className="app-rail-item"><IconButton label={t("home")} onClick={onChat}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M20 14a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h13z" /></svg></IconButton><small>{t("home")}</small></span>
      <span className="app-rail-item"><IconButton label={t("recentConversations")} onClick={onRecents}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M8 6h12M8 12h12M8 18h12M3 6h.01M3 12h.01M3 18h.01" /></svg></IconButton><small>{t("recentShort")}</small></span>
      <span className="app-rail-item"><IconButton label={t("kanaloaConfig")} onClick={onKanaloaSettings}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><circle cx="7.5" cy="15.5" r="3.5" /><path d="M10 13 19 4l2 2-2.5 2.5L20 10l-2.5 2.5L16 11l-3.5 3.5" /></svg></IconButton><small>{t("kanaloa")}</small></span>
      <span className="app-rail-item"><IconButton label={t("agentSettings")} onClick={onSettings}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06A1.7 1.7 0 0 0 15 19.4a1.7 1.7 0 0 0-1 .6 1.7 1.7 0 0 0-.4 1.1V21h-4v-.09A1.7 1.7 0 0 0 8.6 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-.6-1 1.7 1.7 0 0 0-1.1-.4H3v-4h.09A1.7 1.7 0 0 0 4.6 8.6l-.34-1.88-.06-.06 2.83-2.83.06.06A1.7 1.7 0 0 0 9 4.6a1.7 1.7 0 0 0 1-.6 1.7 1.7 0 0 0 .4-1.1V3h4v.09A1.7 1.7 0 0 0 15.4 4.6a1.7 1.7 0 0 0 1.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0 0 19.4 9c.4.25.76.6 1 1 .25.4.39.86.4 1.33V13h-.09A1.7 1.7 0 0 0 19.4 15z" /></svg></IconButton><small>{t("agentSettings")}</small></span>
    </div>
    <span className={`app-rail-status ${online === null ? "syncing" : online ? "online" : "offline"}`} title={t(online === null ? "syncing" : online ? "connected" : "disconnected")} aria-label={t(online === null ? "syncing" : online ? "connected" : "disconnected")} />
  </nav>;
}
