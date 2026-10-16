/**
 * useWebSocketChat
 *
 * Manages a WebSocket connection to /ws/chat.
 * Falls back transparently to HTTP POST /api/chat/message when:
 *   - WebSocket is not supported by the browser
 *   - WebSocket connection fails / is closed unexpectedly
 *   - Server returns an error frame
 *
 * Usage:
 *   const ws = useWebSocketChat({ sessionId, hotelCode, channel });
 *   await ws.sendMessage(text, metadata);   // same signature as before
 *   ws.status  // "connecting" | "open" | "closed" | "http-fallback"
 *   ws.serverHealthy  // whether the BACKEND can serve a turn (from the server's
 *                     // connect/pong frames). Independent of ws.status: the
 *                     // socket opens and pongs happily with the DB down.
 */

import { useCallback, useEffect, useRef, useState } from "react";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type WsStatus = "connecting" | "open" | "closed" | "http-fallback";

export interface ChatApiResponse {
  session_id?: string;
  conversation_id?: string;
  message?: string;
  display_message?: string;
  state?: string;
  success?: boolean;
  suggested_actions?: string[];
  service_llm_label?: string;
  service_llm_confidence?: number;
  metadata?: Record<string, unknown>;
  form_fields?: Array<Record<string, unknown>>;
  form_service_id?: string;
}

export interface UseWebSocketChatOptions {
  sessionId: string;
  hotelCode: string;
  channel?: string;
  /**
   * Public deployment key, present only when running as a CUSTOMER EMBED (the
   * loader puts it in the iframe URL). Forwarded on the socket and on the HTTP
   * fallback so the server can look up this deployment's pinned model. The
   * model itself is never sent from here — the server reads it from the
   * deployment row — so this can't be used to choose a model. The admin harness
   * has no key, which is exactly what keeps its model dropdown working while
   * leaving customer embeds untouchable.
   */
  widgetKey?: string;
  /** Stable conversation/room identifier (UUID). Sent on every message. */
  conversationId?: string;
  /** Current phase (pre_booking, during_stay, etc.) — added to metadata */
  phase?: string;
  /** Called when a full response arrives (WebSocket or HTTP fallback) */
  onResponse: (data: ChatApiResponse) => void;
  /** Called while bot is "thinking" (typing indicator) */
  onTyping?: () => void;
  /** Called on any send error */
  onError?: (detail: string) => void;
  /** Called when a system_handover frame arrives (bot→agent transition or agent→bot) */
  onHandover?: (frame: Record<string, unknown>) => void;
  /** Called when an agent_message frame arrives (agent sent a message to the guest) */
  onAgentMessage?: (frame: Record<string, unknown>) => void;
  /** Called when an agent_typing frame arrives */
  onAgentTyping?: () => void;
  /**
   * Base URL of the Kebo API server, e.g. "https://kebo.kepsla.ai"
   * REQUIRED when the widget is embedded on a third-party hotel website.
   * Falls back to same-origin if omitted (works in admin panel / iframe).
   */
  apiUrl?: string;
  /** Disable WebSocket and always use HTTP */
  forceHttp?: boolean;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function buildWsUrl(
  sessionId: string,
  hotelCode: string,
  channel: string,
  baseUrl?: string,
  widgetKey?: string,
): string {
  let wsBase: string;

  if (baseUrl) {
    // Explicit server URL provided (required when widget is embedded on a hotel's website)
    // Accept http/https → convert to ws/wss; or ws/wss directly
    wsBase = baseUrl
      .replace(/^https:\/\//i, "wss://")
      .replace(/^http:\/\//i, "ws://")
      .replace(/\/$/, ""); // strip trailing slash
  } else {
    // Same-origin fallback — only correct when widget is served from the Kebo server itself
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    wsBase = `${proto}://${window.location.host}`;
  }

  const params = new URLSearchParams({
    session_id: sessionId,
    hotel_code: hotelCode,
    channel,
  });
  // Embed only — identifies the deployment so the server can apply its model pin.
  if (widgetKey) params.set("widget_key", widgetKey);
  return `${wsBase}/ws/chat?${params.toString()}`;
}

const PING_INTERVAL_MS = 25_000;
const PONG_TIMEOUT_MS = 10_000; // no pong within this after a ping → socket is dead
const CONNECT_TIMEOUT_MS = 5_000; // if not open within 5s → fallback
// Delivery ack: the server sends {type:"typing"} the instant it receives a
// message. If no typing/response arrives within this window the socket is a
// zombie (looks open but silently dropped the frame) → resend over HTTP.
const DELIVERY_ACK_TIMEOUT_MS = 15_000;
// Acked but no answer in this window → re-check. If the socket is still healthy
// the server is just slow (local LLM turns can take 15-30s+), so we keep waiting
// rather than resending — resending would double-run the turn on the server and
// compete for its event loop, making everything slower. We only recover over
// HTTP when the socket is actually gone, or after this many patience windows.
const ANSWER_TIMEOUT_MS = 45_000;
const ANSWER_MAX_WAITS = 2; // ~3×45s of patience while the socket stays healthy
// Reconnect backoff: exponential with jitter, capped — avoids hammering the
// server / draining the battery on a flapping mobile network.
const RECONNECT_BASE_MS = 1_000;
const RECONNECT_CAP_MS = 30_000;

function reconnectDelayMs(attempt: number): number {
  const exp = Math.min(RECONNECT_CAP_MS, RECONNECT_BASE_MS * 2 ** attempt);
  return Math.round(exp * (0.8 + Math.random() * 0.4)); // ±20% jitter
}

function newMessageId(): string {
  try {
    if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
  } catch {
    /* noop */
  }
  return `m_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
}

interface PendingSend {
  message: string;
  metadata: Record<string, unknown>;
  ackTimer: ReturnType<typeof setTimeout>;
  answerTimer: ReturnType<typeof setTimeout>;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useWebSocketChat(options: UseWebSocketChatOptions) {
  const {
    sessionId,
    hotelCode,
    channel = "web_widget",
    conversationId,
    phase,
    apiUrl,
    widgetKey,
    onResponse,
    onTyping,
    onError,
    onHandover,
    onAgentMessage,
    onAgentTyping,
    forceHttp = false,
  } = options;

  const [status, setStatus] = useState<WsStatus>("connecting");
  // Whether the BACKEND can actually serve a turn, as reported by the server on
  // the connect frame and on every pong. An open socket proves only that the
  // handshake worked: with the database down the socket still opens and still
  // pongs, which is how the widget came to claim "Online" through an outage.
  const [serverHealthy, setServerHealthy] = useState(true);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttemptRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pingTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pongTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const connectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const isMountedRef = useRef(true);
  const statusRef = useRef<WsStatus>("connecting");
  // In-flight sends awaiting a delivery ack / answer, keyed by message_id.
  const pendingRef = useRef<Map<string, PendingSend>>(new Map());
  // Message ids already delivered to the consumer. A turn can be answered by
  // either the WebSocket response frame or an HTTP recovery (same id → backend
  // dedupe cache); whichever lands first wins and the other is ignored here so
  // the consumer never renders the same answer twice.
  const settledRef = useRef<Set<string>>(new Set());

  // Latest option callbacks kept in a ref so the socket lifecycle (connect /
  // reconnect) never depends on caller-supplied callback identity. Without this,
  // a consumer that passes fresh inline callbacks each render would trigger a
  // reconnect storm. Updated on every render.
  const cbRef = useRef({ onResponse, onTyping, onError, onHandover, onAgentMessage, onAgentTyping });
  cbRef.current = { onResponse, onTyping, onError, onHandover, onAgentMessage, onAgentTyping };

  // Keep statusRef in sync
  const updateStatus = useCallback((s: WsStatus) => {
    statusRef.current = s;
    setStatus(s);
  }, []);

  // Read the server's health verdict off a "connected" / "pong" frame. An older
  // server sends neither field; treat its silence as healthy so a stale backend
  // doesn't start reporting a permanent outage.
  const applyHealthFrame = useCallback((frame: Record<string, unknown>) => {
    if (!("healthy" in frame)) return;
    const healthy = frame.healthy !== false;
    if (!healthy) {
      console.warn("[WS] backend reports degraded  detail=%s", String(frame.detail || ""));
    }
    setServerHealthy(healthy);
  }, []);

  // ── Pending-send timer bookkeeping ──────────────────────────────────────
  // Clear the delivery-ack timer (and optionally everything) for a message id.
  const clearAckTimer = useCallback((messageId?: string) => {
    if (messageId) {
      const p = pendingRef.current.get(messageId);
      if (p) clearTimeout(p.ackTimer);
      return;
    }
    // No id echoed (older server) → clear all ack timers.
    pendingRef.current.forEach((p) => clearTimeout(p.ackTimer));
  }, []);

  const clearPending = useCallback((messageId?: string) => {
    if (messageId) {
      const p = pendingRef.current.get(messageId);
      if (p) {
        clearTimeout(p.ackTimer);
        clearTimeout(p.answerTimer);
        pendingRef.current.delete(messageId);
      }
      return;
    }
    pendingRef.current.forEach((p) => {
      clearTimeout(p.ackTimer);
      clearTimeout(p.answerTimer);
    });
    pendingRef.current.clear();
  }, []);

  // Deliver an answer to the consumer exactly once per message id. Guards the
  // race where both the WebSocket response and an HTTP recovery resolve the
  // same turn (see settledRef). When no id is known (older server frame), it
  // always delivers — there's nothing to dedupe against.
  const deliverResponse = useCallback(
    (messageId: string | undefined, data: ChatApiResponse) => {
      if (messageId) {
        if (settledRef.current.has(messageId)) return;
        settledRef.current.add(messageId);
      }
      clearPending(messageId);
      // An answer landed — whatever the last beat said, the bot is serving.
      setServerHealthy(true);
      cbRef.current.onResponse(data);
    },
    [clearPending],
  );

  // ── HTTP fallback ──────────────────────────────────────────────────────
  const sendViaHttp = useCallback(
    async (
      message: string,
      metadata: Record<string, unknown> = {},
      messageId?: string,
    ): Promise<void> => {
      const mid = messageId || newMessageId();
      const enrichedMeta = {
        ...metadata,
        ...(phase ? { phase } : {}),
        ...(conversationId ? { conversation_id: conversationId } : {}),
        message_id: mid,
      };
      const payload = {
        session_id: sessionId,
        conversation_id: conversationId,
        message,
        message_type: "text",
        hotel_code: hotelCode,
        channel,
        message_id: mid,
        visitor_last_msg_time: Date.now().toString(),
        metadata: enrichedMeta,
      };
      console.info("MESSAGE SENT TO BACKEND (USER) [HTTP]", { message_id: mid, ...payload });
      const httpBase = apiUrl ? apiUrl.replace(/\/$/, "") : "";
      const response = await fetch(`${httpBase}/api/chat/message`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          // Embed only. Carries the deployment identity onto the HTTP retry path
          // so a WS-timeout fallback still answers on the customer's pinned
          // model instead of whatever the harness dropdown currently says.
          ...(widgetKey ? { "X-Widget-Key": widgetKey } : {}),
        },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      const data = (await response.json()) as ChatApiResponse;
      console.info("[WS] Message received from backend [HTTP]", data);
      deliverResponse(mid, data);
    },
    [sessionId, hotelCode, channel, conversationId, phase, apiUrl, widgetKey, deliverResponse],
  );

  // Send over HTTP and always settle the pending entry afterwards. Reuses the
  // same message id so the backend dedupes if the WS copy actually landed.
  const safeHttpFallback = useCallback(
    async (message: string, metadata: Record<string, unknown>, messageId: string): Promise<void> => {
      try {
        await sendViaHttp(message, metadata, messageId);
      } catch (err) {
        cbRef.current.onError?.(err instanceof Error ? err.message : "Failed to send message");
      } finally {
        clearPending(messageId);
      }
    },
    [sendViaHttp, clearPending],
  );

  // ── Ping / heartbeat ────────────────────────────────────────────────────
  const startPing = useCallback(() => {
    if (pingTimerRef.current) clearInterval(pingTimerRef.current);
    pingTimerRef.current = setInterval(() => {
      const ws = wsRef.current;
      if (ws?.readyState !== WebSocket.OPEN) return;
      try {
        ws.send(JSON.stringify({ type: "ping" }));
      } catch {
        return;
      }
      // Arm a one-shot pong watchdog. The "pong" handler clears it; if it fires,
      // the socket is half-open (zombie) → close it so reconnect kicks in.
      if (pongTimerRef.current) clearTimeout(pongTimerRef.current);
      pongTimerRef.current = setTimeout(() => {
        // A turn in flight means the SERVER is busy producing the answer: its
        // per-connection receive loop is blocked inside orchestration and
        // legitimately can't pong yet. The socket is healthy, not dead — closing
        // it here would drop the about-to-arrive answer onto a dead connection
        // (the "taking longer than usual" bug). Keep it; the 45s answer-timeout
        // + HTTP recovery is the backstop if the socket is genuinely gone.
        if (pendingRef.current.size > 0) {
          console.warn("[WS] pong overdue but a turn is in flight — keeping socket (server busy)");
          return;
        }
        console.warn("[WS] pong timeout — closing dead socket");
        try {
          ws.close();
        } catch {
          /* noop */
        }
      }, PONG_TIMEOUT_MS);
    }, PING_INTERVAL_MS);
  }, []);

  const stopPing = useCallback(() => {
    if (pingTimerRef.current) {
      clearInterval(pingTimerRef.current);
      pingTimerRef.current = null;
    }
    if (pongTimerRef.current) {
      clearTimeout(pongTimerRef.current);
      pongTimerRef.current = null;
    }
  }, []);

  // ── Connect ───────────────────────────────────────────────────────────
  const connect = useCallback(() => {
    if (!isMountedRef.current) return;
    if (forceHttp || !sessionId || typeof WebSocket === "undefined") {
      updateStatus("http-fallback");
      return;
    }

    // Clean up any existing socket
    if (wsRef.current) {
      wsRef.current.onopen = null;
      wsRef.current.onmessage = null;
      wsRef.current.onerror = null;
      wsRef.current.onclose = null;
      try { wsRef.current.close(); } catch (_) { /* noop */ }
      wsRef.current = null;
    }

    stopPing();
    updateStatus("connecting");

    let ws: WebSocket;
    try {
      ws = new WebSocket(buildWsUrl(sessionId, hotelCode, channel, apiUrl, widgetKey));
    } catch (err) {
      console.warn("[WS] failed to construct WebSocket — falling back to HTTP", err);
      updateStatus("http-fallback");
      return;
    }
    wsRef.current = ws;

    // Connect timeout: if not open within CONNECT_TIMEOUT_MS → use HTTP fallback
    connectTimeoutRef.current = setTimeout(() => {
      if (statusRef.current === "connecting") {
        console.warn("[WS] connect timeout — switching to HTTP fallback");
        updateStatus("http-fallback");
        try { ws.close(); } catch (_) { /* noop */ }
      }
    }, CONNECT_TIMEOUT_MS);

    ws.onopen = () => {
      if (!isMountedRef.current) return;
      if (connectTimeoutRef.current) {
        clearTimeout(connectTimeoutRef.current);
        connectTimeoutRef.current = null;
      }
      reconnectAttemptRef.current = 0;
      updateStatus("open");
      startPing();
      console.info("[ChatProvider] ✅ WebSocket connected, setting up subscription...");
      console.info("[WS] connected  session_id=%s", sessionId);
    };

    ws.onmessage = (event) => {
      if (!isMountedRef.current) return;
      let frame: Record<string, unknown>;
      try {
        frame = JSON.parse(String(event.data)) as Record<string, unknown>;
      } catch {
        return;
      }
      const type = String(frame.type || "");
      const frameMsgId = String(frame.message_id || "");
      if (type === "typing") {
        console.info("[WS] Typing indicator received  session_id=%s id=%s", sessionId, frameMsgId);
        // Delivery confirmed by the server → cancel the resend-over-HTTP timer.
        clearAckTimer(frameMsgId || undefined);
        cbRef.current.onTyping?.();
      } else if (type === "response") {
        const responseData = frame.data as ChatApiResponse;
        console.info("[WS] Message received from backend", responseData);
        deliverResponse(frameMsgId || undefined, responseData);
      } else if (type === "pong") {
        // Heartbeat reply → socket is alive; cancel the pong watchdog.
        if (pongTimerRef.current) {
          clearTimeout(pongTimerRef.current);
          pongTimerRef.current = null;
        }
        applyHealthFrame(frame);
      } else if (type === "error") {
        console.warn("[WS] Error frame received  detail=%s", frame.detail);
        clearPending(frameMsgId || undefined);
        // The server could not produce an answer — say so on the indicator
        // instead of leaving it green. The next pong re-decides.
        setServerHealthy(false);
        cbRef.current.onError?.(String(frame.detail || "Server error"));
      } else if (type === "system_handover") {
        cbRef.current.onHandover?.(frame);
      } else if (type === "agent_message") {
        cbRef.current.onAgentMessage?.(frame);
      } else if (type === "agent_typing") {
        cbRef.current.onAgentTyping?.();
      } else if (type === "connected") {
        // The connect frame carries the same verdict the pong does, so the
        // indicator is honest from the first second rather than for a whole
        // ping interval.
        applyHealthFrame(frame);
      }
    };

    ws.onerror = () => {
      // onerror fires before onclose; log but let onclose drive reconnect
      console.warn("[WS] socket error session_id=%s", sessionId);
    };

    ws.onclose = (event) => {
      if (!isMountedRef.current) return;
      stopPing();
      if (connectTimeoutRef.current) {
        clearTimeout(connectTimeoutRef.current);
        connectTimeoutRef.current = null;
      }
      // Normal closure (1000/1001) or component unmount → don't reconnect
      if (event.code === 1000 || event.code === 1001 || !isMountedRef.current) {
        updateStatus("closed");
        return;
      }

      // Offline-aware: if the device reports no network, don't loop reconnects
      // (it would just hammer and drain battery). Wait for the "online" /
      // visibility listener to trigger a fresh connect. Sends use HTTP meanwhile.
      if (typeof navigator !== "undefined" && navigator.onLine === false) {
        console.warn("[WS] closed while offline — pausing reconnect until online");
        updateStatus("connecting");
        return;
      }

      // Abnormal closure → reconnect with exponential backoff + jitter (capped).
      // We keep trying (no permanent give-up): while not "open", sends go over
      // HTTP, so messaging is never blocked by a down socket.
      const attempt = reconnectAttemptRef.current;
      const delay = reconnectDelayMs(attempt);
      console.warn("[WS] closed (code=%d) — reconnecting in %dms (attempt %d)", event.code, delay, attempt + 1);
      updateStatus("connecting");
      reconnectAttemptRef.current += 1;
      reconnectTimerRef.current = setTimeout(connect, delay);
    };
  }, [sessionId, hotelCode, channel, apiUrl, widgetKey, forceHttp, startPing, stopPing, updateStatus, applyHealthFrame, clearAckTimer, clearPending, deliverResponse]);

  // ── Connect on mount / when sessionId changes ─────────────────────────
  useEffect(() => {
    isMountedRef.current = true;
    connect();

    // Reconnect proactively when the device regains network or the tab becomes
    // visible again — the two most common mobile causes of a half-open socket.
    const reviveIfNeeded = () => {
      if (!isMountedRef.current) return;
      if (statusRef.current === "open") return;
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      reconnectAttemptRef.current = 0;
      connect();
    };
    const onOnline = () => reviveIfNeeded();
    const onVisible = () => {
      if (typeof document !== "undefined" && document.visibilityState === "visible") reviveIfNeeded();
    };
    if (typeof window !== "undefined") window.addEventListener("online", onOnline);
    if (typeof document !== "undefined") document.addEventListener("visibilitychange", onVisible);

    return () => {
      isMountedRef.current = false;
      stopPing();
      clearPending();
      if (typeof window !== "undefined") window.removeEventListener("online", onOnline);
      if (typeof document !== "undefined") document.removeEventListener("visibilitychange", onVisible);
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      if (connectTimeoutRef.current) clearTimeout(connectTimeoutRef.current);
      if (wsRef.current) {
        wsRef.current.onopen = null;
        wsRef.current.onmessage = null;
        wsRef.current.onerror = null;
        wsRef.current.onclose = null;
        try { wsRef.current.close(1000, "component unmounted"); } catch (_) { /* noop */ }
        wsRef.current = null;
      }
    };
  }, [connect, stopPing, clearPending]);

  // ── Public API ────────────────────────────────────────────────────────

  /**
   * Send a message. Uses WebSocket when connected, HTTP otherwise.
   * Always resolves (never throws) — errors are delivered via onError.
   */
  const sendMessage = useCallback(
    async (
      message: string,
      metadata: Record<string, unknown> = {},
    ): Promise<void> => {
      const currentStatus = statusRef.current;
      const messageId = newMessageId();

      // Use WebSocket if open
      if (currentStatus === "open" && wsRef.current?.readyState === WebSocket.OPEN) {
        const enrichedMeta = {
          ...metadata,
          ...(phase ? { phase } : {}),
          // Callers (buildRequestMetadata) set `ui_source_type`, not `source_type`.
          // Fall back to what is already on metadata before defaulting, otherwise a
          // chip click is relabelled "typed_input" and loses its prefetched answer.
          ui_source_type:
            (metadata.source_type as string) || (metadata.ui_source_type as string) || "typed_input",
          ui_source_label:
            (metadata.source_label as string) || (metadata.ui_source_label as string) || "typed_input",
          message_id: messageId,
        };
        const payload = {
          type: "message",
          session_id: sessionId,
          conversation_id: conversationId,
          message,
          message_type: "text",
          hotel_code: hotelCode,
          channel,
          message_id: messageId,
          visitor_last_msg_time: Date.now().toString(),
          metadata: enrichedMeta,
        };
        console.info("MESSAGE SENT TO BACKEND (USER) [WS]", { message_id: messageId, ...payload });
        try {
          wsRef.current.send(JSON.stringify(payload));
        } catch (err) {
          // send() threw → socket is broken; go straight to HTTP (same id).
          console.warn("[WS] send threw — falling back to HTTP", err);
          await safeHttpFallback(message, metadata, messageId);
          return;
        }

        // Arm the delivery-ack + answer safety timers. The server's instant
        // "typing" ack clears the ack timer; the "response" clears both.
        const ackTimer = setTimeout(() => {
          console.warn("[WS] no delivery ack in %dms (id=%s) — socket likely dead; resending over HTTP", DELIVERY_ACK_TIMEOUT_MS, messageId);
          // Kill the zombie socket so the reconnect path runs.
          try { wsRef.current?.close(); } catch { /* noop */ }
          void safeHttpFallback(message, metadata, messageId);
        }, DELIVERY_ACK_TIMEOUT_MS);
        // Answer safety. When the socket is still healthy the server is simply
        // taking its time (a local turn can be 15-30s+). Keep waiting and let
        // the answer arrive ON this socket — do NOT resend over HTTP, which
        // would make the server run the SAME turn a second time and compete for
        // its event loop, dragging the real answer out even further (and tripping
        // the page-level safety timer). Only recover over HTTP when the socket is
        // genuinely gone (reconnect / lost-answer case), or after we've waited a
        // hard cap with no answer at all. The same-id HTTP recovery returns the
        // cached answer via the backend dedupe, so it never double-processes.
        let answerWaits = 0;
        const onAnswerOverdue = () => {
          const sock = wsRef.current;
          const socketHealthy = sock != null && sock.readyState === WebSocket.OPEN;
          if (socketHealthy && answerWaits < ANSWER_MAX_WAITS) {
            answerWaits += 1;
            console.warn("[WS] answer slow but socket healthy (wait %d/%d, id=%s) — still waiting", answerWaits, ANSWER_MAX_WAITS, messageId);
            const entry = pendingRef.current.get(messageId);
            if (entry) entry.answerTimer = setTimeout(onAnswerOverdue, ANSWER_TIMEOUT_MS);
            return;
          }
          console.warn("[WS] answer not delivered (id=%s socketHealthy=%s) — recovering over HTTP", messageId, socketHealthy);
          void safeHttpFallback(message, metadata, messageId);
        };
        const answerTimer = setTimeout(onAnswerOverdue, ANSWER_TIMEOUT_MS);
        pendingRef.current.set(messageId, { message, metadata, ackTimer, answerTimer });
        return;
      }

      // Not open → HTTP (request/response cannot silently die).
      await safeHttpFallback(message, metadata, messageId);
    },
    [sessionId, hotelCode, channel, conversationId, phase, safeHttpFallback, clearPending],
  );

  /**
   * Force-close the WebSocket and switch to HTTP-only mode.
   * Useful for environments that don't support WebSocket.
   */
  const switchToHttp = useCallback(() => {
    stopPing();
    if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
    if (wsRef.current) {
      try { wsRef.current.close(1000); } catch (_) { /* noop */ }
      wsRef.current = null;
    }
    updateStatus("http-fallback");
  }, [stopPing, updateStatus]);

  return { status, serverHealthy, sendMessage, switchToHttp };
}
