"use client";

import { useEffect, useState } from "react";

import { useLocale } from "@/lib/i18n/LocaleProvider";
import { apiGet } from "@/lib/api";

type HealthSummary = {
  status: "ok" | "warn" | "error" | "unknown";
  label: string;
};

async function fetchHealth(): Promise<HealthSummary> {
  try {
    const h = await apiGet<{ status?: string }>("/health");
    const s = (h.status ?? "ok").toLowerCase();
    if (s === "ok") return { status: "ok", label: "API OK" };
    return { status: "warn", label: s };
  } catch {
    return { status: "error", label: "API offline" };
  }
}

function LanguageToggle() {
  const { locale, setLocale, t } = useLocale();
  const next = locale === "zh" ? "en" : "zh";
  return (
    <button
      type="button"
      onClick={() => setLocale(next)}
      title={t("topbar.switch_language")}
      aria-label={t("topbar.switch_language")}
      className="flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-[var(--kane-topbar-text)] hover:bg-white/10 transition-colors"
    >
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden
      >
        <circle cx="12" cy="12" r="10" />
        <path d="M2 12h20" />
        <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
      </svg>
      <span>{locale === "zh" ? "中" : "EN"}</span>
    </button>
  );
}

export function TopBar() {
  const { t } = useLocale();
  const [health, setHealth] = useState<HealthSummary>({ status: "unknown", label: "…" });

  useEffect(() => {
    let cancelled = false;
    const check = () => {
      fetchHealth().then((h) => {
        if (!cancelled) setHealth(h);
      }).catch(() => {
        if (!cancelled) setHealth({ status: "error", label: "API offline" });
      });
    };
    check();
    const id = setInterval(check, 10000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  const dot =
    health.status === "ok"
      ? "bg-emerald-400"
      : health.status === "warn"
      ? "bg-amber-400"
      : health.status === "error"
      ? "bg-rose-500"
      : "bg-zinc-400";

  return (
    <header className="kane-topbar-surface relative flex h-[86px] items-center justify-between overflow-hidden border-b border-[rgba(255,246,231,0.18)] px-10 shadow-[0_10px_24px_rgba(47,22,8,0.18)]">
      <div className="min-w-0 truncate text-[21px] font-semibold text-[var(--kane-topbar-text)] drop-shadow-[0_0_10px_rgba(255,237,206,0.42)]">
        Kane vNext
      </div>

      <div className="flex items-center gap-3">
        <div
          title={`API: ${health.label}`}
          className="flex items-center gap-1.5 rounded-full border border-white/20 bg-white/10 px-3 py-1 text-xs text-[var(--kane-topbar-text)] backdrop-blur-sm"
        >
          <span className={`inline-block h-2 w-2 rounded-full ${dot}`} />
          <span className="font-mono text-[11px]">{health.label}</span>
        </div>
        <LanguageToggle />
      </div>
    </header>
  );
}
