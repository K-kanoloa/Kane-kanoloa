"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiRequestError, streamTurn, type Message, type TurnDetail, type TurnStatus } from "./api";

export type Connection = "loading" | "live" | "reconnecting" | "disconnected" | "settled";
const terminal = (status: TurnStatus) => ["finished", "failed", "interrupted"].includes(status);

export function useTurn(conversationId: string | null, turnId: string | null) {
  const [detail, setDetail] = useState<TurnDetail | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [connection, setConnection] = useState<Connection>("loading");
  const [error, setError] = useState<unknown>(null);
  const [revision, setRevision] = useState(0);
  const viewed = useRef("");
  const refresh = useCallback(() => setRevision(value => value + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    let retryTimer: ReturnType<typeof setTimeout>;
    let pollTimer: ReturnType<typeof setInterval>;
    let currentStatus: TurnStatus | undefined;
    let snapshotVersion = 0;
    let fetching = false;
    const key = `${conversationId}:${turnId}`;
    if (viewed.current !== key) {
      setDetail(null);
      setMessages([]);
      viewed.current = key;
    }
    setError(null);
    setConnection("loading");

    const load = async (preserveStream = false) => {
      if (!conversationId) return;
      const version = snapshotVersion;
      const [next, history] = await Promise.all([
        turnId ? api.turn(turnId, signal) : Promise.resolve(null),
        api.messages(conversationId, turnId, signal),
      ]);
      if (signal.aborted) return;
      // A newer live event always wins over a slower HTTP read.
      if (preserveStream && version !== snapshotVersion) return;
      currentStatus = next?.status;
      setDetail(previous => preserveStream && next && previous && !terminal(next.status)
        ? { ...next, partial_output: previous.partial_output }
        : next);
      setMessages(history);
      setError(null);
      return next;
    };

    const syncFacts = async () => {
      if (fetching || signal.aborted) return;
      fetching = true;
      try { await load(true); }
      catch (err) { if (!signal.aborted) setError(err); }
      finally { fetching = false; }
    };

    const listen = async () => {
      let attempts = 0;
      while (!signal.aborted) {
        try {
          const loaded = await load();
          if (!turnId || (loaded && terminal(loaded.status))) {
            setConnection("settled");
            return;
          }
          await streamTurn(turnId, signal, (type, raw) => {
            if (signal.aborted) return;
            snapshotVersion += 1;
            const data = raw as Record<string, unknown>;
            if (type === "snapshot") {
              currentStatus = data.status as TurnStatus;
              setDetail(previous => previous ? { ...previous, status: data.status as TurnStatus, partial_output: data.partial_output as string | null } : previous);
              setConnection("live");
              attempts = 0;
              return;
            }
            const payload = (data.payload ?? {}) as Record<string, unknown>;
            if (type === "delta" && typeof payload.delta === "string") {
              setDetail(previous => previous ? { ...previous, partial_output: (previous.partial_output ?? "") + payload.delta, last_event_at: String(data.created_at) } : previous);
              return;
            }
            if (type === "raw") return;
            const safePayload: Record<string, string> = {};
            for (const key of type === "thinking" ? ["status"] : ["status", "tool", "boundary"]) {
              if (typeof payload[key] === "string") safePayload[key] = payload[key];
            }
            if (type === "status_change" && typeof payload.status === "string") currentStatus = payload.status as TurnStatus;
            setDetail(previous => previous ? {
              ...previous,
              status: currentStatus ?? previous.status,
              interrupt_reason: typeof payload.reason === "string" ? payload.reason : previous.interrupt_reason,
              last_event_at: String(data.created_at),
              events: [...previous.events, { event_id: String(data.event_id), event_type: type, created_at: String(data.created_at), payload: safePayload }].slice(-40),
            } : previous);
            if (type === "status_change") void syncFacts();
          });
          if (signal.aborted) return;
          await load();
          if (currentStatus && terminal(currentStatus)) {
            setConnection("settled");
            return;
          }
          throw new Error("Event stream disconnected");
        } catch (err) {
          if (signal.aborted) return;
          setError(err);
          if (err instanceof ApiRequestError && [401, 403, 404].includes(err.status)) {
            setConnection("disconnected");
            return;
          }
          attempts += 1;
          setConnection(attempts > 3 ? "disconnected" : "reconnecting");
          await new Promise<void>(resolve => {
            const onAbort = () => { clearTimeout(retryTimer); resolve(); };
            signal.addEventListener("abort", onAbort, { once: true });
            retryTimer = setTimeout(() => { signal.removeEventListener("abort", onAbort); resolve(); }, Math.min(1000 * 2 ** (attempts - 1), 8000));
          });
        }
      }
    };

    if (conversationId) {
      void listen();
      // Refresh read-only projections (permission / loop facts have no own SSE event).
      pollTimer = setInterval(() => { if (currentStatus && !terminal(currentStatus)) void syncFacts(); }, 3000);
    } else setConnection("settled");
    return () => { controller.abort(); clearTimeout(retryTimer); clearInterval(pollTimer); };
  }, [conversationId, turnId, revision]);

  return { detail, messages, connection, error, refresh };
}
