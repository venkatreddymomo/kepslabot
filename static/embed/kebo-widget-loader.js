(function () {
  "use strict";

  var LOCAL_WIDGET_WIDTH = 380;
  var LOCAL_WIDGET_HEIGHT = 620;

  // ---------------------------------------------------------------------
  // Universal embeddable chat widget loader.
  //
  // Usage (production):
  //   <script src="https://your-host/static/embed/kebo-widget-loader.js"
  //           data-widget-key="wk_abc123" async></script>
  //
  // Usage (MVP / legacy):
  //   <script ... data-hotel-code="sarovar" data-brand-color="#FF6816"></script>
  //
  // Behavior:
  //   1. Read configuration from script's data-* attributes.
  //   2. Fetch /api/widget/bootstrap to get server-driven theme + iframe URL.
  //   3. Mount a fixed-position launcher button + iframe into document.body.
  //   4. data-* attrs override server values (debug/override path).
  //   5. If bootstrap fails, log a warning and don't render — never crash host.
  // ---------------------------------------------------------------------

  function findLoaderScript() {
    if (document.currentScript) return document.currentScript;
    var scripts = document.getElementsByTagName("script");
    for (var i = scripts.length - 1; i >= 0; i -= 1) {
      var src = String(scripts[i].src || "");
      if (/kebo-widget-loader\.js(?:\?|$)/i.test(src)) return scripts[i];
    }
    return null;
  }

  function parseNumber(value, fallback, min, max) {
    var num = Number(value);
    if (!Number.isFinite(num)) return fallback;
    return Math.min(max, Math.max(min, Math.round(num)));
  }

  function parseBoolean(value, fallback) {
    var raw = String(value == null ? "" : value).trim().toLowerCase();
    if (!raw) return fallback;
    if (raw === "1" || raw === "true" || raw === "yes" || raw === "on") return true;
    if (raw === "0" || raw === "false" || raw === "no" || raw === "off") return false;
    return fallback;
  }

  function normalizePosition(value) {
    return String(value || "").trim().toLowerCase() === "left" ? "left" : "right";
  }

  function safeOrigin(value, fallback) {
    try {
      return new URL(String(value || ""), fallback).origin;
    } catch (_err) {
      return fallback;
    }
  }

  function absoluteAssetUrl(value) {
    var raw = String(value || "").trim();
    if (!raw) return "";
    try {
      return new URL(raw, hostOrigin).href;
    } catch (_err) {
      return raw;
    }
  }

  // Colours are written into the customer's page, so this REJECTS anything it
  // does not positively recognise rather than passing it through. brand_color
  // comes from the deployment row, and a value that is not really a colour has
  // no business reaching a style on someone else's site.
  var _NAMED_COLORS = /^(transparent|inherit|currentcolor|black|white|red|green|blue|yellow|orange|purple|pink|gray|grey|brown|cyan|magenta|navy|teal|olive|maroon|silver|gold|beige|ivory|coral|salmon|khaki|lavender|turquoise|indigo|violet)$/i;

  function normalizeColor(value, fallback) {
    var raw = String(value || "").trim();
    if (!raw) return fallback;
    if (/^[0-9a-fA-F]{3,8}$/.test(raw)) return "#" + raw;
    if (/^#[0-9a-fA-F]{3,8}$/.test(raw)) return raw;
    // rgb()/rgba()/hsl()/hsla() with digits, commas, dots, percent and spaces only
    if (/^(rgb|hsl)a?\(\s*[0-9.,%\s/]+\)$/i.test(raw)) return raw;
    if (_NAMED_COLORS.test(raw)) return raw;
    return fallback;
  }

  function detectHostDevice() {
    // The widget can't tell a phone from a laptop on its own: inside the iframe
    // window.innerWidth is the FRAME's ~380px, so it read "mobile" everywhere —
    // which hid the suggestion chips behind a scrollbar-less horizontal scroller
    // on desktop. We run in the HOST page, so we can measure the real viewport.
    // A coarse pointer distinguishes a tablet from a narrow desktop window.
    var vw = Math.floor(
      window.innerWidth || document.documentElement.clientWidth || 1024
    );
    var coarse = false;
    try {
      coarse = window.matchMedia("(pointer: coarse)").matches;
    } catch (_err) {
      coarse = false;
    }
    if (vw < 640) return "mobile";
    return vw < 768 && coarse ? "mobile" : "desktop";
  }

  function warn(msg) {
    try { console.warn("[kebo-widget]", msg); } catch (_e) { /* noop */ }
  }

  function parseJsonObject(value) {
    var raw = String(value == null ? "" : value).trim();
    if (!raw) return null;
    try {
      var parsed = JSON.parse(raw);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed;
      }
    } catch (_err) {
      return null;
    }
    return null;
  }

  // ---- Stable visitor session id (owned by the first-party host page) ----
  // The widget runs inside a CROSS-ORIGIN iframe, whose storage the browser
  // partitions or blocks. That means the widget cannot reliably persist its
  // own session id: each turn can reach the server as a brand-new session,
  // wiping the conversation's location scope (guest picks a city, then the
  // next question is answered brand-wide / for the wrong property).
  //
  // The LOADER runs in the HOST page's first-party context, where storage
  // works normally, so it get-or-creates the session id here and hands it to
  // the iframe via the frame URL. The widget pins an explicit ?session_id=
  // ahead of its own storage, so this makes the session stable across turns,
  // reloads and iframe remounts — independent of iframe-storage partitioning.
  var _memSessionId = "";
  function getOrCreateSessionId(scopeKey) {
    var storageKey = "kebo_sid_" + (String(scopeKey || "default").trim() || "default");
    try {
      var stored = (window.localStorage.getItem(storageKey) || "").trim();
      var low = stored.toLowerCase();
      if (stored && low !== "null" && low !== "undefined") {
        _memSessionId = stored;
        return stored;
      }
    } catch (_e) { /* storage blocked even first-party — fall through */ }
    if (_memSessionId) return _memSessionId;
    var rnd;
    try {
      rnd = (window.crypto && window.crypto.randomUUID)
        ? window.crypto.randomUUID()
        : (Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 12));
    } catch (_e2) {
      rnd = Date.now().toString(36) + "_" + Math.random().toString(36).slice(2, 12);
    }
    var fresh = "sid_" + rnd;
    _memSessionId = fresh;
    try { window.localStorage.setItem(storageKey, fresh); } catch (_e3) { /* keep in-memory */ }
    return fresh;
  }

  // ---- Conversation id lifecycle (mirrors ChatHarness, first-party) -------
  // Same rules the harness uses, but owned by the host page so it survives the
  // iframe's partitioned storage:
  //   • resume the stored conversation on every load,
  //   • rotate ONLY when the guest clicks the dustbin — never on idle/new tab.
  // The session id stays stable across all of it (visitor identity); only the
  // conversation id rotates. Both are per-browser and auto-generated.
  var _memConvId = "";
  function _convStateKey(sid) { return "nb_convo_state_" + sid; }
  function _convLiveKey(sid) { return "nb_convo_live_" + sid; }
  function _newConversationId() {
    return Date.now().toString(16) + "-" +
      Math.random().toString(16).slice(2, 10) + "-" +
      Math.random().toString(16).slice(2, 10);
  }
  function persistConvActivity(sid, convId) {
    if (convId) _memConvId = convId;
    if (!_memConvId || !sid) return;
    try {
      window.localStorage.setItem(
        _convStateKey(sid),
        JSON.stringify({ conversationId: _memConvId, lastActivityTs: Date.now() })
      );
    } catch (_e) { /* best-effort */ }
    try { window.sessionStorage.setItem(_convLiveKey(sid), _memConvId); } catch (_e2) { /* best-effort */ }
  }
  function getOrCreateConversationId(sid) {
    var storedConvId = "", lastActivityTs = 0, liveConvId = "";
    try {
      var s = JSON.parse(window.localStorage.getItem(_convStateKey(sid)) || "{}");
      storedConvId = String(s.conversationId || "").trim();
      lastActivityTs = Number(s.lastActivityTs || 0);
    } catch (_e) { /* fall through to a fresh conversation */ }
    try { liveConvId = String(window.sessionStorage.getItem(_convLiveKey(sid)) || "").trim(); } catch (_e2) { /* noop */ }
    // Resume the stored conversation whenever we have one; it changes only when
    // the guest clicks the dustbin (which posts a fresh id we then persist).
    var convId = storedConvId || _memConvId || _newConversationId();
    persistConvActivity(sid, convId);
    return convId;
  }

  var script = findLoaderScript();
  if (!script) return;

  var data = script.dataset || {};
  var scriptUrl = safeOrigin(script.src || window.location.href, window.location.origin);
  var hostOrigin = safeOrigin(data.host || scriptUrl, window.location.origin);

  // ---- Warm the connection to our host.
  // Runs at parse time, BEFORE the bootstrap fetch: on a customer's domain every
  // request we make is cross-origin, so the DNS lookup + TCP + TLS handshake was
  // being paid at bootstrap time and again for the iframe document. Preconnect
  // gets that out of the way while the host page is still rendering.
  (function primeHostConnection() {
    try {
      if (hostOrigin === window.location.origin) return;
      var head = document.head || document.getElementsByTagName("head")[0];
      if (!head) return;
      ["preconnect", "dns-prefetch"].forEach(function (rel) {
        var link = document.createElement("link");
        link.rel = rel;
        link.href = hostOrigin;
        if (rel === "preconnect") link.crossOrigin = "anonymous";
        head.appendChild(link);
      });
    } catch (_err) { /* never break the host page */ }
  })();

  // ---- Step 1: assemble the override config from data-* ----------------
  // Anything the operator supplies on the script tag wins over server bootstrap.
  var overrides = {
    widgetKey: String(data.widgetKey || "").trim(),
    hotelCode: String(data.hotelCode || data.propertyCode || data.widgetId || "").trim().toLowerCase(),
    phase: String(data.phase || "").trim().toLowerCase(),
    position: data.position ? normalizePosition(data.position) : null,
    width: null,
    height: null,
    offset: parseNumber(data.offset, 20, 8, 64),
    zIndex: parseNumber(data.zIndex, 2147482000, 1000, 2147483647),
    brandColor: data.brandColor ? normalizeColor(data.brandColor, null) : null,
    accentColor: data.accentColor ? normalizeColor(data.accentColor, null) : null,
    bgColor: data.bgColor ? normalizeColor(data.bgColor, null) : null,
    textColor: data.textColor ? normalizeColor(data.textColor, null) : null,
    botName: data.botName ? String(data.botName).trim() : null,
    autoOpen: data.autoOpen != null ? parseBoolean(data.autoOpen, false) : null,
    sessionId: data.sessionId ? String(data.sessionId).trim() : null,
    guestId: data.guestId ? String(data.guestId).trim() : null,
    entityId: data.entityId ? String(data.entityId).trim() : null,
    organisationId: data.organisationId ? String(data.organisationId).trim() : null,
    roomNumber: data.roomNumber ? String(data.roomNumber).trim() : null,
    guestPhone: data.guestPhone ? String(data.guestPhone).trim() : null,
    guestName: data.guestName ? String(data.guestName).trim() : null,
    groupId: data.groupId ? String(data.groupId).trim() : null,
    ticketSource: data.ticketSource ? String(data.ticketSource).trim() : null,
    flow: data.flow ? String(data.flow).trim() : null,
    disablePrefetch: data.disablePrefetch != null ? parseBoolean(data.disablePrefetch, false) : null,
    extraMetadata: parseJsonObject(data.extraMetadata || data.metadata || ""),
    sessionTtlHours: data.sessionTtlHours ? parseNumber(data.sessionTtlHours, 24, 1, 720) : null,
    idleTimeoutMinutes: data.idleTimeoutMinutes != null ? parseNumber(data.idleTimeoutMinutes, 30, 0, 720) : null,
  };

  if (!overrides.widgetKey && !overrides.hotelCode) {
    warn("widget loader requires data-widget-key or data-hotel-code; aborting");
    return;
  }

  // ---- Step 2: fetch bootstrap, then mount -----------------------------

  function fetchBootstrap() {
    var url = new URL("/api/widget/bootstrap", hostOrigin);
    if (overrides.widgetKey) {
      url.searchParams.set("widget_key", overrides.widgetKey);
    } else {
      url.searchParams.set("hotel_code", overrides.hotelCode);
    }
    return fetch(url.toString(), {
      method: "GET",
      credentials: "include",
      headers: { "Accept": "application/json" },
    }).then(function (resp) {
      if (!resp.ok) throw new Error("bootstrap " + resp.status);
      return resp.json();
    });
  }

  function mergeConfig(boot) {
    var theme = (boot && boot.theme) || {};
    // Per-browser identity, owned first-party (see getOrCreateSessionId). An
    // explicit data-session-id still wins if an operator sets one, but the
    // default is a unique auto-generated id per visitor's browser.
    var idScope = overrides.widgetKey || overrides.hotelCode || (boot && boot.hotel_code) || "default";
    var resolvedSessionId = overrides.sessionId || getOrCreateSessionId(idScope);
    var resolvedConversationId = getOrCreateConversationId(resolvedSessionId);
    return {
      widgetKey: overrides.widgetKey || boot.widget_key || "",
      hotelCode: overrides.hotelCode || boot.hotel_code || "default",
      phase: overrides.phase || boot.phase || "pre_booking",
      position: overrides.position || boot.position || "right",
      width: LOCAL_WIDGET_WIDTH,
      height: LOCAL_WIDGET_HEIGHT,
      offset: overrides.offset,
      zIndex: overrides.zIndex,
      brandColor: overrides.brandColor || theme.brand_color || "#FF6816",
      accentColor: overrides.accentColor || theme.accent_color || theme.brand_color || "#FF6816",
      bgColor: overrides.bgColor || theme.bg_color || "#FFFFFF",
      textColor: overrides.textColor || theme.text_color || "#1A1A2E",
      botName: overrides.botName || boot.bot_name || "Assistant",
      // Absolutised against the Kebo host: the configured value is normally a
      // server-relative path ("/static/branding/..."), and the launcher renders
      // it on the HOST page, where a relative src would resolve against the
      // customer's own domain and 404.
      logoUrl: absoluteAssetUrl(overrides.logoUrl || boot.logo_url || ""),
      autoOpen: overrides.autoOpen != null ? overrides.autoOpen : !!boot.auto_open,
      sessionId: resolvedSessionId,
      conversationId: resolvedConversationId,
      guestId: overrides.guestId || "",
      entityId: overrides.entityId || "",
      organisationId: overrides.organisationId || "",
      roomNumber: overrides.roomNumber || "",
      guestPhone: overrides.guestPhone || "",
      guestName: overrides.guestName || "",
      groupId: overrides.groupId || "",
      ticketSource: overrides.ticketSource || "",
      flow: overrides.flow || "",
      disablePrefetch: overrides.disablePrefetch,
      extraMetadata: overrides.extraMetadata || null,
      sessionTtlHours: overrides.sessionTtlHours != null ? overrides.sessionTtlHours : (boot.session_ttl_hours || 24),
      idleTimeoutMinutes: overrides.idleTimeoutMinutes != null
        ? overrides.idleTimeoutMinutes
        : (boot.idle_timeout_minutes != null ? boot.idle_timeout_minutes : 30),
      iframeUrl: boot.iframe_url || (hostOrigin + "/chat"),
    };
  }

  function buildFrameUrl(config) {
    var frameUrl;
    try {
      frameUrl = new URL(config.iframeUrl, hostOrigin);
    } catch (_e) {
      frameUrl = new URL("/chat", hostOrigin);
    }
    frameUrl.searchParams.set("embed", "1");
    if (config.widgetKey) frameUrl.searchParams.set("widget_key", config.widgetKey);
    frameUrl.searchParams.set("hotel_code", config.hotelCode);
    frameUrl.searchParams.set("phase", config.phase);
    frameUrl.searchParams.set("brand_color", config.brandColor);
    frameUrl.searchParams.set("accent_color", config.accentColor);
    frameUrl.searchParams.set("bg_color", config.bgColor);
    frameUrl.searchParams.set("text_color", config.textColor);
    frameUrl.searchParams.set("bot_name", config.botName);
    if (config.logoUrl) frameUrl.searchParams.set("logo_url", config.logoUrl);
    frameUrl.searchParams.set("position", config.position);
    frameUrl.searchParams.set("width", String(config.width));
    frameUrl.searchParams.set("height", String(config.height));
    // The widget can't tell a phone from a laptop on its own: inside the iframe
    // window.innerWidth is the FRAME's ~380px, so it read "mobile" everywhere.
    // We're in the host page, so we can measure the real viewport and say.
    frameUrl.searchParams.set("device", detectHostDevice());
    if (config.sessionId) frameUrl.searchParams.set("session_id", config.sessionId);
    if (config.conversationId) frameUrl.searchParams.set("conversation_id", config.conversationId);
    if (config.guestId) frameUrl.searchParams.set("guest_id", config.guestId);
    if (config.entityId) frameUrl.searchParams.set("entity_id", config.entityId);
    if (config.organisationId) frameUrl.searchParams.set("organisation_id", config.organisationId);
    if (config.roomNumber) frameUrl.searchParams.set("room_number", config.roomNumber);
    if (config.guestPhone) frameUrl.searchParams.set("guest_phone", config.guestPhone);
    if (config.guestName) frameUrl.searchParams.set("guest_name", config.guestName);
    if (config.groupId) frameUrl.searchParams.set("group_id", config.groupId);
    if (config.ticketSource) frameUrl.searchParams.set("ticket_source", config.ticketSource);
    if (config.flow) frameUrl.searchParams.set("flow", config.flow);
    if (config.disablePrefetch != null) {
      frameUrl.searchParams.set("disable_prefetch", config.disablePrefetch ? "1" : "0");
    }
    if (config.extraMetadata) {
      frameUrl.searchParams.set("extra_metadata", JSON.stringify(config.extraMetadata));
    }
    frameUrl.searchParams.set("session_ttl_hours", String(config.sessionTtlHours || 24));
    if (config.idleTimeoutMinutes != null) {
      frameUrl.searchParams.set("idle_timeout_minutes", String(config.idleTimeoutMinutes));
    }
    return frameUrl.toString();
  }

  function mountWidget(config) {
    var root = document.createElement("div");
    root.setAttribute("data-kebo-widget-root", "true");
    root.style.position = "fixed";
    root.style.zIndex = String(config.zIndex);
    root.style.pointerEvents = "none";
    root.style.width = config.width + "px";
    root.style.height = config.height + "px";
    root.style.overflow = "visible";
    var requestedWidth = config.width;
    var requestedHeight = config.height;
    var teaserDismissed = Boolean(config.autoOpen);

    var frameContainer = document.createElement("div");
    frameContainer.setAttribute("data-kebo-widget-frame", "true");
    frameContainer.style.position = "absolute";
    frameContainer.style.bottom = "0";
    frameContainer.style.width = config.width + "px";
    frameContainer.style.height = config.height + "px";
    frameContainer.style.maxWidth = "min(calc(100vw - 16px), 600px)";
    frameContainer.style.maxHeight = "min(calc(100vh - 48px), 900px)";
    frameContainer.style.background = config.bgColor;
    frameContainer.style.border = "1px solid rgba(15, 23, 42, 0.12)";
    frameContainer.style.borderRadius = "16px";
    frameContainer.style.boxShadow = "0 24px 60px rgba(15, 23, 42, 0.30)";
    frameContainer.style.overflow = "hidden";
    frameContainer.style.pointerEvents = "none";
    frameContainer.style.opacity = "0";
    frameContainer.style.transform = "translateY(14px) scale(0.985)";
    frameContainer.style.transformOrigin = config.position === "left" ? "bottom left" : "bottom right";
    frameContainer.style.transition = "opacity 180ms ease, transform 180ms ease";
    if (config.position === "left") frameContainer.style.left = "0";
    else frameContainer.style.right = "0";

    var iframe = document.createElement("iframe");
    iframe.src = buildFrameUrl(config);
    iframe.title = config.botName + " Chat Widget";
    // NOT loading="lazy". The frame sits at opacity 0 until the launcher is
    // clicked, and lazy told the browser to defer it — so the ENTIRE React
    // bundle only began downloading at click time and the guest waited through
    // it. Loading it up front costs the host page nothing visible and makes the
    // click feel instant.
    iframe.setAttribute("allow", "clipboard-read; clipboard-write");
    iframe.style.display = "block";
    iframe.style.width = "100%";
    iframe.style.height = "100%";
    iframe.style.border = "0";
    iframe.style.background = config.bgColor;
    frameContainer.appendChild(iframe);

    var teaser = document.createElement("div");
    teaser.setAttribute("data-kebo-widget-teaser", "true");
    teaser.style.position = "absolute";
    teaser.style.bottom = "72px";
    teaser.style.maxWidth = "220px";
    teaser.style.padding = "10px 12px";
    teaser.style.background = "#ffffff";
    teaser.style.border = "1px solid rgba(15, 23, 42, 0.12)";
    teaser.style.borderRadius = "12px";
    teaser.style.boxShadow = "0 12px 28px rgba(15, 23, 42, 0.18)";
    teaser.style.transition = "opacity 180ms ease, transform 180ms ease";
    teaser.style.transformOrigin = config.position === "left" ? "bottom left" : "bottom right";
    teaser.style.opacity = "0";
    teaser.style.transform = "translateY(8px) scale(0.98)";
    teaser.style.pointerEvents = "none";
    if (config.position === "left") teaser.style.left = "0";
    else teaser.style.right = "0";


    var teaserTitle = document.createElement("div");
    teaserTitle.style.fontSize = "12px";
    teaserTitle.style.fontWeight = "600";
    teaserTitle.style.color = "#0f172a";
    // Same wave the in-app teaser leads with.
    teaserTitle.textContent = "👋 Hi there!";

    var teaserBody = document.createElement("div");
    teaserBody.style.marginTop = "2px";
    teaserBody.style.fontSize = "11px";
    teaserBody.style.lineHeight = "1.35";
    teaserBody.style.color = "#64748b";
    teaserBody.textContent = "Need help? Chat with " + config.botName;

    teaser.appendChild(teaserTitle);
    teaser.appendChild(teaserBody);

    var launcher = document.createElement("button");
    launcher.type = "button";
    launcher.setAttribute("aria-label", "Open chat");
    launcher.setAttribute("data-kebo-widget-launcher", "true");
    launcher.style.display = "flex";
    launcher.style.alignItems = "center";
    launcher.style.justifyContent = "center";
    launcher.style.width = "58px";
    launcher.style.height = "58px";
    launcher.style.position = "absolute";
    launcher.style.bottom = "0";
    launcher.style.border = "0";
    launcher.style.borderRadius = "999px";
    launcher.style.cursor = "pointer";
    launcher.style.pointerEvents = "auto";
    launcher.style.background = config.brandColor;
    launcher.style.boxShadow = "0 16px 34px rgba(15, 23, 42, 0.34)";
    launcher.style.transition = "transform 140ms ease, opacity 140ms ease";
    launcher.style.overflow = "hidden";
    launcher.style.padding = "0";
    var LAUNCHER_CHAT_ICON = '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true"><path d="M12 3C7.03 3 3 6.58 3 11c0 2.1.9 4.02 2.38 5.45L4.4 21l4.8-1.58c.9.22 1.84.33 2.8.33 4.97 0 9-3.58 9-8s-4.03-8-9-8Zm0 14.5c-.84 0-1.67-.11-2.45-.34l-.58-.17-2.25.74.48-2.08-.4-.42A6.05 6.05 0 0 1 5 11c0-3.1 3.13-5.75 7-5.75S19 7.9 19 11s-3.13 6.5-7 6.5Z" fill="white"/></svg>';
    launcher.innerHTML = LAUNCHER_CHAT_ICON;
    // A configured logo becomes the launcher face (same avatar the header shows).
    // Any load failure falls back to the built-in chat glyph so the host page
    // never ends up with an empty button.
    if (config.logoUrl) {
      var launcherLogo = document.createElement("img");
      launcherLogo.alt = "";
      launcherLogo.setAttribute("aria-hidden", "true");
      launcherLogo.style.width = "100%";
      launcherLogo.style.height = "100%";
      launcherLogo.style.borderRadius = "999px";
      launcherLogo.style.objectFit = "cover";
      launcherLogo.style.display = "block";
      launcherLogo.onerror = function () {
        launcher.innerHTML = LAUNCHER_CHAT_ICON;
      };
      launcherLogo.src = config.logoUrl;
      launcher.innerHTML = "";
      launcher.appendChild(launcherLogo);
    }
    if (config.position === "left") {
      launcher.style.left = "0";
      launcher.style.right = "auto";
    } else {
      launcher.style.right = "0";
      launcher.style.left = "auto";
    }

    root.appendChild(frameContainer);
    root.appendChild(teaser);
    root.appendChild(launcher);

    var isOpen = Boolean(config.autoOpen);
    // The live conversation id. Starts as the one we handed the iframe; the
    // widget posts a "conversation" event when it rotates (e.g. the dustbin
    // "new chat"), so our first-party copy stays authoritative for reloads.
    var activeConversationId = config.conversationId || "";

    function applyState() {
      var showTeaser = !isOpen && !teaserDismissed;
      if (isOpen) {
        frameContainer.style.opacity = "1";
        frameContainer.style.pointerEvents = "auto";
        frameContainer.style.transform = "translateY(0) scale(1)";
        launcher.style.opacity = "0";
        launcher.style.pointerEvents = "none";
        launcher.style.transform = "scale(0.9)";
      } else {
        frameContainer.style.opacity = "0";
        frameContainer.style.pointerEvents = "none";
        frameContainer.style.transform = "translateY(14px) scale(0.985)";
        launcher.style.opacity = "1";
        launcher.style.pointerEvents = "auto";
        launcher.style.transform = "scale(1)";
      }

      if (showTeaser) {
        teaser.style.opacity = "1";
        teaser.style.pointerEvents = "auto";
        teaser.style.transform = "translateY(0) scale(1)";
      } else {
        teaser.style.opacity = "0";
        teaser.style.pointerEvents = "none";
        teaser.style.transform = "translateY(8px) scale(0.98)";
      }
    }

    function openWidget() { isOpen = true; teaserDismissed = true; applyState(); }
    function closeWidget() { isOpen = false; applyState(); }
    function toggleWidget() { isOpen = !isOpen; applyState(); }

    function setRequestedSize(nextWidth, nextHeight) {
      var changed = false;
      if (Number.isFinite(nextWidth)) {
        requestedWidth = parseNumber(nextWidth, requestedWidth, 260, 600);
        changed = true;
      }
      if (Number.isFinite(nextHeight)) {
        requestedHeight = parseNumber(nextHeight, requestedHeight, 56, 900);
        changed = true;
      }
      if (changed) applyAnchorAndSize();
    }

    function onMessage(event) {
      if (event.origin !== hostOrigin) return;
      if (event.source !== iframe.contentWindow) return;
      var payload = event.data;
      if (!payload || typeof payload !== "object") return;
      if (payload.source !== "kebo-widget") return;
      // Refresh the conversation's activity clock so idle-rotation matches the
      // harness (rotate only after 30 min of no messages, not 30 min of page).
      if (payload.type === "widget:activity" || payload.type === "activity") {
        persistConvActivity(config.sessionId, activeConversationId);
        return;
      }
      // The widget rotated its conversation id (dustbin "new chat", idle, etc.).
      // Adopt it as authoritative and persist first-party, so a later reload
      // resumes the CURRENT thread — not the one we originally handed over.
      if (payload.type === "widget:conversation" || payload.type === "conversation") {
        var cid = String(payload.conversation_id || "").trim();
        if (cid) { activeConversationId = cid; persistConvActivity(config.sessionId, cid); }
        return;
      }
      if (payload.type === "widget:close" || payload.type === "close") return closeWidget();
      if (payload.type === "widget:open" || payload.type === "open") return openWidget();
      if (payload.type === "widget:toggle" || payload.type === "toggle") return toggleWidget();
      if (payload.type === "widget:resize" || payload.type === "widget:size" || payload.type === "resize") {
        return setRequestedSize(Number(payload.width), Number(payload.height));
      }
    }

    function onKeyDown(event) {
      if (event.key === "Escape" && isOpen) closeWidget();
    }

    function getViewportWidth() {
      return Math.max(320, Math.floor(window.innerWidth || document.documentElement.clientWidth || 1024));
    }
    function getViewportHeight() {
      // The VISUAL viewport, which shrinks when the on-screen keyboard opens.
      // window.innerHeight does not, and getBottomOffset() below lifts the panel
      // by the keyboard's height regardless — so on a phone the panel was pushed
      // clean off the top of the screen and the form it contained could not be
      // scrolled back into view without dismissing the keyboard first.
      var vv = window.visualViewport;
      if (vv && vv.height) {
        return Math.max(240, Math.floor(vv.height));
      }
      return Math.max(360, Math.floor(window.innerHeight || document.documentElement.clientHeight || 768));
    }
    function getBottomOffset() {
      var base = config.offset;
      var vv = window.visualViewport;
      if (!vv) return base;
      var keyboardDelta = Math.max(0, (window.innerHeight || vv.height) - vv.height - vv.offsetTop);
      if (!Number.isFinite(keyboardDelta) || keyboardDelta <= 0) return base;
      return Math.max(base, base + Math.round(keyboardDelta));
    }

    // Keep the frame's idea of the device in sync when the host page is
    // resized or a tablet is rotated. Only posts on an actual change.
    var lastDevice = detectHostDevice();
    function pushDeviceIfChanged() {
      var next = detectHostDevice();
      if (next === lastDevice) return;
      lastDevice = next;
      try {
        if (iframe && iframe.contentWindow) {
          // Targeted at our own origin rather than "*": the payload is only a
          // device hint, but a wildcard target means any document that ends up
          // in that frame receives it.
          iframe.contentWindow.postMessage(
            { type: "kebo:device", device: next },
            hostOrigin
          );
        }
      } catch (_err) {
        /* frame not ready yet — the URL param already carried the initial value */
      }
    }

    function applyAnchorAndSize() {
      pushDeviceIfChanged();
      var vw = getViewportWidth();
      var vh = getViewportHeight();
      // Reserve the gutter on BOTH sides. Capping at `vw - 16` while anchoring
      // `offset` (20px) from the right pushed the frame past the left edge of
      // every phone narrower than ~400px — 375px screens lost 5px of the panel.
      var width = Math.min(requestedWidth, Math.max(260, vw - config.offset * 2));
      var availableHeight = Math.max(56, vh - 48);
      var height = requestedHeight <= 80
        ? 56
        : Math.min(requestedHeight, availableHeight);
      root.style.width = width + "px";
      root.style.height = height + "px";
      frameContainer.style.width = width + "px";
      frameContainer.style.height = height + "px";
      root.style.bottom = getBottomOffset() + "px";
      if (config.position === "left") {
        root.style.left = config.offset + "px";
        root.style.right = "auto";
      } else {
        root.style.right = config.offset + "px";
        root.style.left = "auto";
      }
    }

    function destroyWidget() {
      window.removeEventListener("message", onMessage);
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", applyAnchorAndSize);
      if (window.visualViewport) {
        window.visualViewport.removeEventListener("resize", applyAnchorAndSize);
        window.visualViewport.removeEventListener("scroll", applyAnchorAndSize);
      }
      if (root.parentNode) root.parentNode.removeChild(root);
      if (window.KeboWidget === api) delete window.KeboWidget;
    }

    launcher.addEventListener("click", openWidget);
    window.addEventListener("message", onMessage);
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", applyAnchorAndSize, { passive: true });
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", applyAnchorAndSize, { passive: true });
      window.visualViewport.addEventListener("scroll", applyAnchorAndSize, { passive: true });
    }

    function mountRoot() {
      if (!document.body) return;
      document.body.appendChild(root);
      applyAnchorAndSize();
      applyState();
    }

    if (document.body) mountRoot();
    else window.addEventListener("DOMContentLoaded", mountRoot, { once: true });

    var api = {
      version: "2.0.0",
      config: config,
      open: openWidget,
      close: closeWidget,
      toggle: toggleWidget,
      resize: setRequestedSize,
      destroy: destroyWidget,
      isOpen: function () { return isOpen; },
    };
    window.KeboWidget = api;
  }

  // ---- Bootstrap orchestration ----------------------------------------
  fetchBootstrap()
    .then(function (boot) { mountWidget(mergeConfig(boot)); })
    .catch(function (err) {
      warn("bootstrap failed: " + (err && err.message ? err.message : err));
      // Fallback: if the operator supplied enough overrides, render anyway.
      if (overrides.brandColor || overrides.hotelCode) {
        mountWidget(mergeConfig({}));
      }
    });
})();
