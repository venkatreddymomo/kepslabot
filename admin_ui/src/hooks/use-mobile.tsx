import * as React from "react";

const MOBILE_BREAKPOINT = 768;

/**
 * True when the guest is on a PHONE — not merely when the viewport is narrow.
 *
 * The widget renders inside an iframe the loader sizes to ~380px, so measuring
 * `window.innerWidth` here reported "mobile" on every embed, laptops included.
 * That silently flipped the chip row to the phone layout (a horizontal scroller
 * with the scrollbar hidden, so chips ran off-screen with no affordance), split
 * the inline form into phone steps, and shortened the "Book a Stay" CTA — all on
 * desktop.
 *
 * Resolution order:
 *   1. `?device=mobile|desktop` — the loader measures the PARENT viewport and
 *      passes it in, and re-sends it on resize via postMessage. Authoritative.
 *   2. Inside an iframe with no hint: `(pointer: coarse)` reports the real input
 *      device regardless of frame width, so a direct <iframe> embed that skips
 *      the loader still resolves correctly.
 *   3. Top-level page: the plain viewport width, as before.
 */
export function useIsMobile() {
  const [isMobile, setIsMobile] = React.useState<boolean | undefined>(undefined);

  React.useEffect(() => {
    const readParamHint = (): boolean | null => {
      try {
        const value = new URLSearchParams(window.location.search)
          .get("device")
          ?.trim()
          .toLowerCase();
        if (value === "mobile") return true;
        if (value === "desktop") return false;
      } catch {
        /* malformed query string — fall through to the other signals */
      }
      return null;
    };

    const inIframe = (() => {
      try {
        return window.parent !== window;
      } catch {
        // Cross-origin parent access throws — that itself means we're framed.
        return true;
      }
    })();

    // The loader can update this after a parent resize (desktop <-> phone
    // rotation), so keep it in a ref-ish local that resolve() re-reads.
    let pushedHint: boolean | null = null;

    const resolve = () => {
      const hint = pushedHint ?? readParamHint();
      if (hint !== null) {
        setIsMobile(hint);
        return;
      }
      if (inIframe) {
        // `window.innerWidth` here is the FRAME (~380px), so it always says
        // mobile. `screen.width` reports the physical DEVICE and is unaffected
        // by framing: a phone reports ~360-430, a laptop 1280+.
        //
        // A previous attempt used `(pointer: coarse)`, which is true on any
        // touchscreen laptop — so those still got the phone layout (chips in a
        // scrollbar-less horizontal strip). Screen width doesn't have that flaw.
        const deviceWidth = window.screen?.width || window.innerWidth;
        setIsMobile(deviceWidth < MOBILE_BREAKPOINT);
        return;
      }
      setIsMobile(window.innerWidth < MOBILE_BREAKPOINT);
    };

    const widthMql = window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT - 1}px)`);
    const pointerMql = window.matchMedia("(pointer: coarse)");
    widthMql.addEventListener("change", resolve);
    pointerMql.addEventListener("change", resolve);

    const onHostMessage = (event: MessageEvent) => {
      const data = event.data as { type?: string; device?: string } | null;
      if (!data || data.type !== "kebo:device") return;
      const value = String(data.device || "").trim().toLowerCase();
      if (value === "mobile") pushedHint = true;
      else if (value === "desktop") pushedHint = false;
      else return;
      resolve();
    };
    window.addEventListener("message", onHostMessage);

    resolve();
    return () => {
      widthMql.removeEventListener("change", resolve);
      pointerMql.removeEventListener("change", resolve);
      window.removeEventListener("message", onHostMessage);
    };
  }, []);

  return !!isMobile;
}
