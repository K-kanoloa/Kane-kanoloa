"use client";

import { History, KeyRound, Languages, MessageSquare, Settings } from "lucide-react";
import { useLocale, useT } from "@/lib/i18n/LocaleProvider";
import { IconButton } from "./workspace/ui";

export function AppRail({ online, active, onChat, onRecents, onKanaloaSettings, onSettings }: {
  online: boolean | null;
  active: "chat" | "recent" | "kanaloa" | "settings";
  onChat: () => void;
  onRecents: () => void;
  onKanaloaSettings: () => void;
  onSettings: () => void;
}) {
  const t = useT();
  const { locale, setLocale } = useLocale();
  const items = [
    { id: "chat", label: "home", short: "home", Icon: MessageSquare, action: onChat },
    { id: "recent", label: "recentConversations", short: "recentShort", Icon: History, action: onRecents },
    { id: "kanaloa", label: "kanaloaConfig", short: "kanaloa", Icon: KeyRound, action: onKanaloaSettings },
    { id: "settings", label: "agentSettings", short: "agentSettings", Icon: Settings, action: onSettings },
  ];
  return <nav className="app-rail" aria-label={t("primaryNavigation")}>
    <div className="app-rail-brand"><img className="app-rail-logo" src="/kane-octopus.png" alt="Kane" width={56} height={56} /></div>
    <div className="app-rail-nav">{items.map(({ id, label, short, Icon, action }) => <span className="app-rail-item" key={id}><IconButton label={t(label)} aria-pressed={active === id} onClick={action}><Icon size={20} /></IconButton><small>{t(short)}</small></span>)}</div>
    <div className="app-rail-bottom"><IconButton label={t("language")} onClick={() => setLocale(locale === "zh" ? "en" : "zh")}><Languages size={18} /></IconButton><span className={`app-rail-status ${online === null ? "syncing" : online ? "online" : "offline"}`} title={t(online === null ? "syncing" : online ? "connected" : "disconnected")} aria-label={t(online === null ? "syncing" : online ? "connected" : "disconnected")} /></div>
  </nav>;
}
