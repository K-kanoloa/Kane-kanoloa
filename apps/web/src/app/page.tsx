"use client";

import { useEffect, useState } from "react";
import { ReadPage } from "@/components/page-shell";
import { apiGet } from "@/lib/api";

type HealthData = {
  status?: string;
  service?: string;
  version?: string;
  startup?: { total_ms?: number };
};

export default function Home() {
  const [health, setHealth] = useState<HealthData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    apiGet<HealthData>("/health")
      .then((data) => {
        if (!cancelled) {
          setHealth(data);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <ReadPage
      title="Kane vNext"
      subtitle="Model-Free Conversation ↔ Agent Harness (Phase 1 Skeleton)"
    >
      <div className="space-y-6">
        {/* Core Architecture Notice */}
        <section className="kane-card rounded-[var(--kane-radius-card)] border border-[var(--kane-border)] bg-[var(--kane-paper)] p-6 shadow-[var(--kane-shadow-card)]">
          <div className="flex items-center gap-3">
            <span
              className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-lg font-bold"
              style={{
                background: "linear-gradient(180deg, #ffe1b8, #ffd092)",
                color: "var(--kane-amber-deep)",
              }}
            >
              ⚡
            </span>
            <div>
              <h2 className="text-base font-semibold text-[var(--kane-walnut)]">
                Phase 1 Pure Deletion &amp; Skeleton Complete
              </h2>
              <p className="text-xs text-[var(--kane-muted)]">
                Legacy Agent OS business core (Task, Run, Memory, Verifier, Skills, Kanaloa) has been removed.
              </p>
            </div>
          </div>

          <div className="mt-5 rounded-md border border-[var(--kane-border)] bg-[var(--kane-page)] p-4 text-xs text-[var(--kane-walnut)] leading-relaxed">
            <p className="font-semibold text-[var(--kane-walnut-deep)]">
              Architecture Boundaries (Phase 1 Freeze):
            </p>
            <ul className="mt-2 list-disc list-inside space-y-1 text-[var(--kane-muted)]">
              <li>API is operating in minimal health-only mode.</li>
              <li>Legacy Task / Run execution and worker queues are completely removed.</li>
              <li>
                <strong>New vNext primitives</strong> (Conversation, Message, Turn, Mailbox, Dispatcher, Adapter main chains) are <strong>not yet implemented</strong> in this phase.
              </li>
              <li>Existing data directories, secrets, and git tags are strictly preserved.</li>
            </ul>
          </div>
        </section>

        {/* Live Service Health */}
        <section className="kane-card rounded-[var(--kane-radius-card)] border border-[var(--kane-border)] bg-[var(--kane-paper)] p-6 shadow-[var(--kane-shadow-card)]">
          <h2 className="text-sm font-semibold text-[var(--kane-walnut)]">
            Service Health Status
          </h2>
          <div className="mt-4 grid grid-cols-1 gap-4 sm:grid-cols-3">
            <div className="rounded-lg border border-[var(--kane-border)] bg-white/50 p-3.5">
              <div className="text-[11px] font-medium text-[var(--kane-muted)] uppercase">API Status</div>
              <div className="mt-1 flex items-center gap-2">
                <span
                  className={`inline-block h-2.5 w-2.5 rounded-full ${
                    loading ? "bg-zinc-400" : health?.status === "ok" ? "bg-emerald-500" : "bg-rose-500"
                  }`}
                />
                <span className="text-sm font-semibold text-[var(--kane-walnut)]">
                  {loading ? "Checking..." : health?.status === "ok" ? "Online" : "Offline"}
                </span>
              </div>
            </div>

            <div className="rounded-lg border border-[var(--kane-border)] bg-white/50 p-3.5">
              <div className="text-[11px] font-medium text-[var(--kane-muted)] uppercase">Version</div>
              <div className="mt-1 text-sm font-semibold text-[var(--kane-walnut)]">
                {health?.version ?? "2.0.0 (Phase 1)"}
              </div>
            </div>

            <div className="rounded-lg border border-[var(--kane-border)] bg-white/50 p-3.5">
              <div className="text-[11px] font-medium text-[var(--kane-muted)] uppercase">Startup Time</div>
              <div className="mt-1 text-sm font-semibold text-[var(--kane-walnut)]">
                {health?.startup?.total_ms != null ? `${health.startup.total_ms} ms` : "n/a"}
              </div>
            </div>
          </div>

          {error && (
            <div className="mt-4 rounded-md border border-rose-200 bg-rose-50 p-3 text-xs text-rose-700">
              API Connection Error: {error}
            </div>
          )}
        </section>
      </div>
    </ReadPage>
  );
}
