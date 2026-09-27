"use client";

import { useLocale } from "@/lib/i18n/LocaleProvider";
import { IconButton } from "./workspace/ui";

export function TopBar({ sidebarOpen, inspectorOpen, online, onSidebar, onInspector }: { sidebarOpen: boolean; inspectorOpen: boolean; online: boolean | null; onSidebar: () => void; onInspector: () => void }) {
  const { locale, setLocale, t } = useLocale();
  return <header className="kane-topbar-surface workspace-topbar">
    <div className="topbar-left"><IconButton label={t(sidebarOpen ? "hideSidebar" : "openConversations")} onClick={onSidebar} aria-expanded={sidebarOpen} aria-controls="conversation-sidebar">☰</IconButton><span className="topbar-title">Kane <span>/</span> {t("conversations")}</span></div>
    <div className="topbar-actions"><span className={`connection-indicator ${online === false ? "offline" : ""}`}><span className="status-dot" />{t(online === null ? "syncing" : online ? "local" : "disconnected")}</span><button className="language-button" onClick={() => setLocale(locale === "zh" ? "en" : "zh")} title={t("language")} aria-label={t("language")}>{locale === "zh" ? "EN" : "中文"}</button><button className="work-toggle" onClick={onInspector} aria-expanded={inspectorOpen} aria-controls="work-inspector" title={t("openWork")}>{t("work")} <span aria-hidden="true">◧</span></button></div>
  </header>;
}
