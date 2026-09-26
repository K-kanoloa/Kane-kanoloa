"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { useT } from "@/lib/i18n/LocaleProvider";

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

export function SidebarNav() {
  const pathname = usePathname();
  const t = useT();

  const isOverview = pathname === "/";

  return (
    <div className="flex h-full flex-col bg-[var(--kane-sidebar)]">
      <div className="flex h-[86px] items-center gap-2 border-b border-[var(--kane-border-strong)] bg-[linear-gradient(180deg,#fff8ee,#ffefd5)] px-3">
        <span
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-base font-bold"
          style={{
            background: "linear-gradient(180deg, #ffe1b8, #ffd092)",
            color: "var(--kane-amber-deep)",
          }}
          aria-hidden
        >
          🐙
        </span>
        <div className="min-w-0 leading-tight">
          <div className="truncate text-sm font-semibold leading-tight text-[var(--kane-walnut)]">Kane</div>
          <div className="truncate text-[10px] leading-tight text-[var(--kane-muted)]">vNext Harness</div>
        </div>
      </div>

      <nav className="flex-1 overflow-y-auto p-2" aria-label="Navigation">
        <ul className="space-y-0.5">
          <li>
            <Link
              href="/"
              aria-current={isOverview ? "page" : undefined}
              title="Overview"
              className={cx(
                "group relative flex items-center gap-2 rounded-md px-2.5 py-2 text-[15px] transition-colors",
                isOverview
                  ? "bg-[var(--kane-amber-soft)] font-semibold text-[var(--kane-walnut)] shadow-[inset_0_0_0_1px_rgba(232,118,19,0.13)]"
                  : "text-[var(--foreground)] hover:bg-white/45 hover:text-[var(--kane-walnut)]"
              )}
            >
              {isOverview ? (
                <span
                  className="absolute bottom-1.5 left-0 top-1.5 w-0.5 rounded-r"
                  style={{ background: "var(--kane-amber)" }}
                  aria-hidden
                />
              ) : null}
              <span
                className={cx(
                  "inline-flex h-5 w-5 shrink-0 items-center justify-center",
                  isOverview ? "text-[var(--kane-amber-deep)]" : "text-[var(--kane-moss)]"
                )}
                aria-hidden
              >
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M4 13h6V4H4zM14 20h6V4h-6zM4 20h6v-3H4z" />
                </svg>
              </span>
              <span className="truncate">Overview</span>
            </Link>
          </li>
        </ul>
      </nav>

      <div className="space-y-1 border-t border-[var(--kane-border)] bg-white/35 px-3 py-2.5">
        <div className="text-[10px] font-medium uppercase text-[var(--kane-muted)]">
          Phase 1 Skeleton
        </div>
        <div className="text-[11px] text-[var(--kane-muted)]">
          Health-only core
        </div>
        <div className="flex items-center gap-1.5 pt-1 text-[10px] text-[var(--kane-muted)]">
          <span
            className="inline-block h-1.5 w-1.5 rounded-full"
            style={{ background: "var(--kane-moss)" }}
            aria-hidden
          />
          vNext
        </div>
      </div>
    </div>
  );
}
