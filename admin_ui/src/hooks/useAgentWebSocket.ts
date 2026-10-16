/**
 * useAgentWebSocket
 *
 * Manages a WebSocket connection to /ws/agent for hotel staff (agents).
 * Handles all agent↔guest frames: new escalations, guest messages, accept/resolve.
 * Phase 5: observer mode, conversation history replay, escalation expiry,
 *           browser notifications, and Web Audio API sound alerts.
 *
 * Usage:
 *   const ws = useAgentWebSocket({ hotelCode, agentId, agentName, ...callbacks });
 *   ws.sendAccept(sessionId)
 *   ws.sendObserve(sessionId)
 *   ws.sendMessage(sessionId, "Hello guest!")
 *   ws.sendTyping(sessionId)
 *   ws.sendResolve(sessionId)
 */

import { useCallback, useEffect, useRef, useState } from "react";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type AgentWsStatus = "connecting" | "open" | "closed" | "error";

export interface PendingEscalation {
  session_id: string;
  preview: string;
  guest_info: {
    room_number?: string;
    guest_name?: string;
    hotel_code?: string;
    guest_phone?: string;
  };
  created_at: string;
  /** undefined = waiting, true = accepted by me, false = accepted by another agent */
  accepted?: boolean;
  accepted_by?: string;
}

export interface AgentChatMessage {
  id: string;
  role: "guest" | "agent" | "system";
  text: string;
  timestamp: string;
}

export interface HistoryMessage {
  role: string;
  content: string;
  timestamp: string;
  metadata: Record<string, unknown>;
}

export interface ActiveSession {
  session_id: string;
  guest_info: PendingEscalation["guest_info"];
  started_at: string;
  message_count: number;
}

export interface UseAgentWebSocketOptions {
  hotelCode: string;
  agentId: string;
  agentName: string;
  /** Base URL for the Kebo server — same origin if omitted */
  apiUrl?: string;
  /** Called when a new escalation comes in */
  onNewEscalation?: (esc: PendingEscalation) => void;
  /** Called when a guest sends a message to an active room */
  onGuestMessage?: (sessionId: string, text: string, timestamp: string) => void;
  /** Called when the guest is typing */
  onGuestTyping?: (sessionId: string) => void;
  /** Called when another agent accepts a pending escalation (or on-connect replay of active rooms) */
  onEscalationAccepted?: (
    sessionId: string,
    byAgent: string,
    guestInfo?: PendingEscalation["guest_info"],
    acceptedAt?: string,
  ) => void;
  /** Called when this agent's accept is confirmed by the server */
  onAccepted?: (sessionId: string, guestInfo: PendingEscalation["guest_info"]) => void;
  /** Called when this agent joins as an observer */
  onObserving?: (sessionId: string, guestInfo: PendingEscalation["guest_info"]) => void;
  /** Called when another observer joins a room this agent is in */
  onObserverJoined?: (sessionId: string, agentName: string, observerCount: number) => void;
  /** Called when a resolve is confirmed by the server */
  onResolved?: (sessionId: string) => void;
  /** Called when an escalation expires (no agent accepted in time) */
  onEscalationExpired?: (sessionId: string, reason: string) => void;
  /** Called when conversation history is replayed (on accept or observe) */
  onHistory?: (sessionId: string, messages: HistoryMessage[]) => void;
  /** Called when an agent message echo arrives (e.g. sent by another agent in room) */
  onAgentMessageEcho?: (sessionId: string, text: string, agentName: string, timestamp: string) => void;
  /** Called when a whisper arrives from another agent */
  onWhisper?: (sessionId: string, text: string, fromAgent: string) => void;
  /** Called on connect with already-active bot-only sessions (replay) */
  onActiveSession?: (session: ActiveSession) => void;
  /** Called when a new guest starts a conversation */
  onSessionStarted?: (session: ActiveSession) => void;
  /** Called when a guest disconnects */
  onSessionEnded?: (sessionId: string) => void;
  /** Called when a guest/bot message arrives for a monitored bot session */
  onSessionMessage?: (sessionId: string, role: "guest" | "bot", text: string, timestamp: string) => void;
  /** Called when the server confirms this agent is monitoring a bot session */
  onMonitoring?: (sessionId: string) => void;
  /** Called when another agent takes over a bot session */
  onSessionTakenOver?: (sessionId: string, byAgent: string) => void;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function buildAgentWsUrl(
  hotelCode: string,
  agentId: string,
  agentName: string,
  baseUrl?: string,
): string {
  let wsBase: string;
  if (baseUrl) {
    wsBase = baseUrl
      .replace(/^https:\/\//i, "wss://")
      .replace(/^http:\/\//i, "ws://")
      .replace(/\/$/, "");
  } else {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    wsBase = `${proto}://${window.location.host}`;
  }
  const params = new URLSearchParams({
    hotel_code: hotelCode,
    agent_id: agentId,
    agent_name: agentName,
  });
  return `${wsBase}/ws/agent?${params.toString()}`;
}

/** Play a short notification beep using the Web Audio API. */
function playNotificationBeep(): void {
  try {
    const ctx = new (window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)();
    const oscillator = ctx.createOscillator();
    const gainNode = ctx.createGain();
    oscillator.connect(gainNode);
    gainNode.connect(ctx.destination);
    oscillator.type = "sine";
    oscillator.frequency.setValueAtTime(880, ctx.currentTime);
    gainNode.gain.setValueAtTime(0.3, ctx.currentTime);
    gainNode.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.4);
    oscillator.start(ctx.currentTime);
    oscillator.stop(ctx.currentTime + 0.4);
    oscillator.onended = () => ctx.close();
  } catch {
    // Audio API unavailable — silent fallback
  }
}

/** Show a browser push notification (requires Notification permission). */
function showBrowserNotification(title: string, body: string): void {
  if (typeof Notification === "undefined") return;
  if (Notification.permission !== "granted") return;
  try {
    new Notification(title, {
      body,
      icon: "/favicon.ico",
      tag: "kebo-escalation",
    });
  } catch {
    // Notification API unavailable
  }
}

/** Request Notification permission. Call on user interaction (e.g. sign-in). */
export async function requestNotificationPermission(): Promise<void> {
  if (typeof Notification === "undefined") return;
  if (Notification.permission === "default") {
    await Notification.requestPermission();
  }
}

const PING_INTERVAL_MS = 25_000;
const RECONNECT_DELAYS_MS = [1_000, 3_000, 8_000];

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useAgentWebSocket(options: UseAgentWebSocketOptions) {
  const {
    hotelCode,
    agentId,
    agentName,
    apiUrl,
    onNewEscalation,
    onGuestMessage,
    onGuestTyping,
    onEscalationAccepted,
    onAccepted,
    onObserving,
    onObserverJoined,
    onResolved,
    onEscalationExpired,
    onHistory,
    onAgentMessageEcho,
    onWhisper,
    onActiveSession,
    onSessionStarted,
    onSessionEnded,
    onSessionMessage,
    onMonitoring,
    onSessionTakenOver,
  } = options;

  const [status, setStatus] = useState<AgentWsStatus>("connecting");

  const wsRef = useRef<WebSocket | null>(null);
  const pingTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectAttemptRef = useRef(0);
  const isMountedRef = useRef(true);

  const stopPing = useCallback(() => {
    if (pingTimerRef.current) {
      clearInterval(pingTimerRef.current);
      pingTimerRef.current = null;
    }
  }, []);

  const startPing = useCallback(() => {
    stopPing();
    pingTimerRef.current = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: "ping" }));
      }
    }, PING_INTERVAL_MS);
  }, [stopPing]);

  const send = useCallback((payload: Record<string, unknown>) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(payload));
    }
  }, []);

  const connect = useCallback(() => {
    if (!isMountedRef.current || !hotelCode || !agentId) return;

    if (wsRef.current) {
      wsRef.current.onopen = null;
      wsRef.current.onmessage = null;
      wsRef.current.onerror = null;
      wsRef.current.onclose = null;
      try { wsRef.current.close(); } catch (_) { /* noop */ }
      wsRef.current = null;
    }
    stopPing();
    setStatus("connecting");

    let ws: WebSocket;
    try {
      ws = new WebSocket(buildAgentWsUrl(hotelCode, agentId, agentName, apiUrl));
    } catch {
      setStatus("error");
      return;
    }
    wsRef.current = ws;

    ws.onopen = () => {
      if (!isMountedRef.current) return;
      reconnectAttemptRef.current = 0;
      setStatus("open");
      startPing();
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

      if (type === "new_escalation") {
        const esc: PendingEscalation = {
          session_id: String(frame.session_id || ""),
          preview: String(frame.preview || ""),
          guest_info: (frame.guest_info as PendingEscalation["guest_info"]) || {},
          created_at: String(frame.created_at || new Date().toISOString()),
        };
        onNewEscalation?.(esc);
        // Alert the agent: beep + browser notification
        playNotificationBeep();
        showBrowserNotification(
          "New Guest Escalation",
          esc.preview || "A guest is requesting assistance.",
        );
      } else if (type === "guest_message") {
        onGuestMessage?.(
          String(frame.session_id || ""),
          String(frame.text || ""),
          String(frame.timestamp || new Date().toISOString()),
        );
      } else if (type === "guest_typing") {
        onGuestTyping?.(String(frame.session_id || ""));
      } else if (type === "escalation_accepted") {
        onEscalationAccepted?.(
          String(frame.session_id || ""),
          String(frame.by_agent || ""),
          (frame.guest_info as PendingEscalation["guest_info"]) || undefined,
          frame.accepted_at ? String(frame.accepted_at) : undefined,
        );
      } else if (type === "accepted") {
        onAccepted?.(
          String(frame.session_id || ""),
          (frame.guest_info as PendingEscalation["guest_info"]) || {},
        );
      } else if (type === "observing") {
        onObserving?.(
          String(frame.session_id || ""),
          (frame.guest_info as PendingEscalation["guest_info"]) || {},
        );
      } else if (type === "observer_joined") {
        onObserverJoined?.(
          String(frame.session_id || ""),
          String(frame.agent_name || ""),
          Number(frame.observer_count || 0),
        );
      } else if (type === "resolved") {
        onResolved?.(String(frame.session_id || ""));
      } else if (type === "escalation_expired") {
        onEscalationExpired?.(
          String(frame.session_id || ""),
          String(frame.reason || ""),
        );
      } else if (type === "history") {
        onHistory?.(
          String(frame.session_id || ""),
          (frame.messages as HistoryMessage[]) || [],
        );
      } else if (type === "agent_message_echo") {
        onAgentMessageEcho?.(
          String(frame.session_id || ""),
          String(frame.text || ""),
          String(frame.agent_name || ""),
          String(frame.timestamp || new Date().toISOString()),
        );
      } else if (type === "whisper") {
        onWhisper?.(
          String(frame.session_id || ""),
          String(frame.text || ""),
          String(frame.from_agent || ""),
        );
      } else if (type === "active_session") {
        onActiveSession?.({
          session_id: String(frame.session_id || ""),
          guest_info: (frame.guest_info as PendingEscalation["guest_info"]) || {},
          started_at: String(frame.started_at || new Date().toISOString()),
          message_count: Number(frame.message_count || 0),
        });
      } else if (type === "session_started") {
        onSessionStarted?.({
          session_id: String(frame.session_id || ""),
          guest_info: (frame.guest_info as PendingEscalation["guest_info"]) || {},
          started_at: String(frame.started_at || new Date().toISOString()),
          message_count: 0,
        });
      } else if (type === "session_ended") {
        onSessionEnded?.(String(frame.session_id || ""));
      } else if (type === "session_message") {
        const role = String(frame.role || "guest") as "guest" | "bot";
        onSessionMessage?.(
          String(frame.session_id || ""),
          role,
          String(frame.text || ""),
          String(frame.timestamp || new Date().toISOString()),
        );
      } else if (type === "monitoring") {
        onMonitoring?.(String(frame.session_id || ""));
      } else if (type === "session_taken_over") {
        onSessionTakenOver?.(
          String(frame.session_id || ""),
          String(frame.by_agent || ""),
        );
      }
      // pong, connected, error, whisper_ack → ignored
    };

    ws.onerror = () => {
      // Let onclose drive reconnect
    };

    ws.onclose = (event) => {
      if (!isMountedRef.current) return;
      stopPing();
      if (event.code === 4001) {
        // Auth error from server (missing hotel_code or agent_id) — don't retry
        setStatus("error");
        return;
      }
      if (event.code === 1000 || event.code === 1001) {
        setStatus("closed");
        return;
      }
      const attempt = reconnectAttemptRef.current;
      if (attempt >= RECONNECT_DELAYS_MS.length) {
        setStatus("error");
        return;
      }
      const delay = RECONNECT_DELAYS_MS[attempt];
      reconnectAttemptRef.current += 1;
      setStatus("connecting");
      reconnectTimerRef.current = setTimeout(connect, delay);
    };
  }, [
    hotelCode, agentId, agentName, apiUrl,
    onNewEscalation, onGuestMessage, onGuestTyping,
    onEscalationAccepted, onAccepted, onObserving, onObserverJoined,
    onResolved, onEscalationExpired, onHistory, onAgentMessageEcho, onWhisper,
    onActiveSession, onSessionStarted, onSessionEnded, onSessionMessage,
    onMonitoring, onSessionTakenOver,
    startPing, stopPing,
  ]);

  useEffect(() => {
    isMountedRef.current = true;
    if (hotelCode && agentId) connect();
    return () => {
      isMountedRef.current = false;
      stopPing();
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      if (wsRef.current) {
        wsRef.current.onopen = null;
        wsRef.current.onmessage = null;
        wsRef.current.onerror = null;
        wsRef.current.onclose = null;
        try { wsRef.current.close(1000, "unmounted"); } catch (_) { /* noop */ }
        wsRef.current = null;
      }
    };
  }, [connect, stopPing]);

  // ── Public API ──────────────────────────────────────────────────────────

  const sendAccept = useCallback((sessionId: string) => {
    send({ type: "accept", session_id: sessionId });
  }, [send]);

  const sendObserve = useCallback((sessionId: string) => {
    send({ type: "observe", session_id: sessionId });
  }, [send]);

  const sendMessage = useCallback((sessionId: string, text: string) => {
    if (!text.trim()) return;
    send({ type: "message", session_id: sessionId, text });
  }, [send]);

  const sendTyping = useCallback((sessionId: string) => {
    send({ type: "typing", session_id: sessionId });
  }, [send]);

  const sendResolve = useCallback((sessionId: string) => {
    send({ type: "resolve", session_id: sessionId });
  }, [send]);

  const sendWhisper = useCallback((sessionId: string, text: string) => {
    if (!text.trim()) return;
    send({ type: "whisper", session_id: sessionId, text });
  }, [send]);

  const sendMonitor = useCallback((sessionId: string) => {
    send({ type: "monitor", session_id: sessionId });
  }, [send]);

  const sendTakeover = useCallback((sessionId: string) => {
    send({ type: "takeover", session_id: sessionId });
  }, [send]);

  return {
    status,
    sendAccept,
    sendObserve,
    sendMessage,
    sendTyping,
    sendResolve,
    sendWhisper,
    sendMonitor,
    sendTakeover,
  };
}
