import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useWebSocketChat } from "./useWebSocketChat";

/**
 * Drives the mobile "stuck message" guarantees:
 *  - a half-open (zombie) socket that looks OPEN but never delivers → resend over HTTP
 *  - a healthy turn → no fallback, no duplicate
 *  - missing pongs → the heartbeat watchdog closes the dead socket
 */

class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  url: string;
  readyState = FakeWebSocket.CONNECTING;
  onopen: ((ev?: unknown) => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: ((ev?: unknown) => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;
  sent: string[] = [];
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
    setTimeout(() => {
      this.readyState = FakeWebSocket.OPEN;
      this.onopen?.();
    }, 0);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({ code: 1006 });
  }

  emit(obj: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(obj) });
  }

  frames(): Array<Record<string, unknown>> {
    return this.sent.map((s) => JSON.parse(s) as Record<string, unknown>);
  }

  messageFrames(): Array<Record<string, unknown>> {
    return this.frames().filter((f) => f.type === "message");
  }
}

const okJson = (body: Record<string, unknown>) => ({
  ok: true,
  status: 200,
  json: async () => body,
});

function makeOptions(overrides: Record<string, unknown> = {}) {
  return {
    sessionId: "sess-1",
    hotelCode: "ZONE",
    onResponse: vi.fn(),
    onTyping: vi.fn(),
    onError: vi.fn(),
    ...overrides,
  };
}

async function openSocket() {
  // Flush the FakeWebSocket constructor's setTimeout(0) so onopen fires.
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  FakeWebSocket.instances = [];
  // @ts-expect-error test shim
  global.WebSocket = FakeWebSocket;
});

afterEach(() => {
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("useWebSocketChat — mobile reliability", () => {
  it("resends over HTTP with the SAME message_id when no delivery ack arrives (zombie socket)", async () => {
    const fetchMock = vi.fn(async () =>
      okJson({ message: "hi", session_id: "sess-1", state: "idle" }),
    );
    global.fetch = fetchMock as unknown as typeof fetch;

    const opts = makeOptions();
    const { result } = renderHook(() => useWebSocketChat(opts));
    await openSocket();
    expect(result.current.status).toBe("open");

    await act(async () => {
      await result.current.sendMessage("tell me about the work desk area");
    });

    const ws = FakeWebSocket.instances[0];
    const wsFrames = ws.messageFrames();
    expect(wsFrames).toHaveLength(1);
    const wsId = String(wsFrames[0].message_id);
    expect(wsId).toBeTruthy();

    // No "typing" ack comes back. fetch must NOT have fired yet (still waiting).
    expect(fetchMock).not.toHaveBeenCalled();

    // Cross the 15s delivery-ack window → resend over HTTP.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(String(url)).toContain("/api/chat/message");
    const body = JSON.parse(String(init.body));
    // Same id → backend dedupe makes the resend safe.
    expect(body.message_id).toBe(wsId);
    // The zombie socket was closed so the reconnect path can run.
    expect(ws.closed).toBe(true);
  });

  it("does NOT fall back when the server acks + answers (no duplicate)", async () => {
    const fetchMock = vi.fn(async () => okJson({ message: "x", session_id: "sess-1", state: "idle" }));
    global.fetch = fetchMock as unknown as typeof fetch;

    const onResponse = vi.fn();
    const opts = makeOptions({ onResponse });
    const { result } = renderHook(() => useWebSocketChat(opts));
    await openSocket();

    await act(async () => {
      await result.current.sendMessage("hello");
    });
    const ws = FakeWebSocket.instances[0];
    const wsId = String(ws.messageFrames()[0].message_id);

    // Server confirms receipt (typing) then answers (response), echoing the id.
    await act(async () => {
      ws.emit({ type: "typing", message_id: wsId });
      ws.emit({ type: "response", message_id: wsId, data: { message: "the desk has…" } });
    });

    // Even well past the delivery-ack + answer windows, no HTTP resend happens.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(onResponse).toHaveBeenCalledWith({ message: "the desk has…" });
  });

  it("stays patient on a slow turn (healthy socket) — no HTTP resend, answer arrives on the socket", async () => {
    // The server is just SLOW (a local LLM turn can take 15-30s+). While the
    // socket is healthy the hook must NOT resend over HTTP — that would make the
    // server run the SAME turn again and compete for its event loop. The answer
    // arrives on the socket and is delivered; fetch is never called.
    const fetchMock = vi.fn(async () => okJson({ message: "x", session_id: "sess-1", state: "idle" }));
    global.fetch = fetchMock as unknown as typeof fetch;

    const onResponse = vi.fn();
    const onError = vi.fn();
    const opts = makeOptions({ onResponse, onError });
    const { result } = renderHook(() => useWebSocketChat(opts));
    await openSocket();

    await act(async () => {
      await result.current.sendMessage("tell me about the hotels in Phalodi");
    });
    const ws = FakeWebSocket.instances[0];
    const wsId = String(ws.messageFrames()[0].message_id);
    await act(async () => {
      ws.emit({ type: "typing", message_id: wsId });
    });

    // Cross two patience windows (~95s) with the socket healthy → still waiting,
    // NO HTTP resend (so the server never double-runs the turn).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(95_000);
    });
    expect(fetchMock).not.toHaveBeenCalled();

    // The slow answer finally arrives ON the socket → delivered, still no resend.
    await act(async () => {
      ws.emit({ type: "response", message_id: wsId, data: { message: "slow but here" } });
    });
    expect(onResponse).toHaveBeenCalledWith({ message: "slow but here" });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
  });

  it("recovers the cached answer over HTTP after the patience cap (answer never arrived)", async () => {
    // Socket stays healthy but the server never delivers. After the patience cap
    // (~3×45s) the hook recovers the cached answer over HTTP with the SAME id.
    const fetchMock = vi.fn(async () =>
      okJson({ message: "Phalodi has two hotels…", session_id: "sess-1", state: "idle" }),
    );
    global.fetch = fetchMock as unknown as typeof fetch;

    const onResponse = vi.fn();
    const onError = vi.fn();
    const opts = makeOptions({ onResponse, onError });
    const { result } = renderHook(() => useWebSocketChat(opts));
    await openSocket();

    await act(async () => {
      await result.current.sendMessage("tell me about the hotels in Phalodi");
    });
    const ws = FakeWebSocket.instances[0];
    const wsId = String(ws.messageFrames()[0].message_id);
    await act(async () => {
      ws.emit({ type: "typing", message_id: wsId });
    });

    // Through the first two patience windows → still waiting, no resend.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(95_000);
    });
    expect(fetchMock).not.toHaveBeenCalled();

    // Past the cap → recover over HTTP with the SAME id.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50_000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body)).message_id).toBe(wsId);
    expect(onResponse).toHaveBeenCalledWith({
      message: "Phalodi has two hotels…",
      session_id: "sess-1",
      state: "idle",
    });
    expect(onError).not.toHaveBeenCalled();
  });

  it("never delivers an answer twice when a late WS response races the HTTP recovery", async () => {
    const fetchMock = vi.fn(async () =>
      okJson({ message: "cached answer", session_id: "sess-1", state: "idle" }),
    );
    global.fetch = fetchMock as unknown as typeof fetch;

    const onResponse = vi.fn();
    const opts = makeOptions({ onResponse });
    const { result } = renderHook(() => useWebSocketChat(opts));
    await openSocket();

    await act(async () => {
      await result.current.sendMessage("hi");
    });
    const ws = FakeWebSocket.instances[0];
    const wsId = String(ws.messageFrames()[0].message_id);

    // Ack only, then let the answer-timer recover over HTTP past the patience cap.
    await act(async () => {
      ws.emit({ type: "typing", message_id: wsId });
      await vi.advanceTimersByTimeAsync(145_000);
    });
    expect(onResponse).toHaveBeenCalledTimes(1);

    // A late WS response for the SAME id arrives after recovery → must be ignored.
    await act(async () => {
      ws.emit({ type: "response", message_id: wsId, data: { message: "late ws answer" } });
    });
    expect(onResponse).toHaveBeenCalledTimes(1);
    expect(onResponse).toHaveBeenCalledWith({
      message: "cached answer",
      session_id: "sess-1",
      state: "idle",
    });
  });

  it("closes a dead socket when pongs stop coming (heartbeat watchdog)", async () => {
    global.fetch = vi.fn(async () => okJson({ message: "x", session_id: "sess-1", state: "idle" })) as unknown as typeof fetch;
    const opts = makeOptions();
    const { result } = renderHook(() => useWebSocketChat(opts));
    await openSocket();
    const ws = FakeWebSocket.instances[0];

    // ~25s → a ping is sent (advance past the boundary to avoid timer flake).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(26_000);
    });
    expect(ws.frames().some((f) => f.type === "ping")).toBe(true);
    expect(ws.closed).toBe(false);

    // No pong; ~10s later the watchdog must close the zombie socket.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(11_000);
    });
    expect(ws.closed).toBe(true);
    // The watchdog must trigger a reconnect → a brand-new socket is created.
    expect(FakeWebSocket.instances.length).toBeGreaterThan(1);
  });

  it("does NOT close the socket on a missed pong while a turn is in flight (server busy)", async () => {
    // The server processes each turn inline on its receive loop, so during a
    // long turn it legitimately can't pong. The healthy-but-busy socket must be
    // kept alive — closing it would drop the about-to-arrive answer (the
    // "taking longer than usual" bug).
    global.fetch = vi.fn(async () => okJson({ message: "x", session_id: "sess-1", state: "idle" })) as unknown as typeof fetch;
    const opts = makeOptions();
    const { result } = renderHook(() => useWebSocketChat(opts));
    await openSocket();
    const ws = FakeWebSocket.instances[0];

    await act(async () => {
      await result.current.sendMessage("a question that takes the server a while");
    });
    const wsId = String(ws.messageFrames()[0].message_id);
    // Server acks receipt (typing) — clears the 15s ack-timer so it won't be the
    // thing that closes the socket — but sends NO answer yet (still working).
    await act(async () => {
      ws.emit({ type: "typing", message_id: wsId });
    });

    // ~25s → ping; then ~11s later the pong watchdog fires. A turn is in flight,
    // so the socket must stay OPEN (and no reconnect happens).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(26_000);
      await vi.advanceTimersByTimeAsync(11_000);
    });
    expect(ws.closed).toBe(false);
    expect(FakeWebSocket.instances.length).toBe(1);
  });

  it("does NOT reconnect-storm when the caller passes unmemoized callbacks", async () => {
    global.fetch = vi.fn(async () => okJson({ message: "x", session_id: "sess-1", state: "idle" })) as unknown as typeof fetch;
    // Fresh inline callbacks every render (a caller that forgot useCallback).
    const { result, rerender } = renderHook(() =>
      useWebSocketChat({
        sessionId: "sess-1",
        hotelCode: "ZONE",
        onResponse: () => {},
        onTyping: () => {},
        onError: () => {},
      }),
    );
    await openSocket();
    // Force several re-renders with brand-new callback identities.
    for (let i = 0; i < 5; i++) {
      rerender();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });
    }
    // The socket lifecycle must not depend on callback identity → exactly one socket.
    expect(FakeWebSocket.instances.length).toBe(1);
    expect(result.current.status).toBe("open");
  });
});
