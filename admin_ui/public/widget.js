/**
 * Kebo Chat Widget Loader
 * ========================
 * Paste this script on any hotel website to embed the Kebo chat widget.
 *
 * Minimal usage:
 *   <script
 *     src="https://kebo.kepsla.ai/widget.js"
 *     data-hotel-code="khil"
 *     data-api-url="https://kebo.kepsla.ai"
 *   ></script>
 *
 * All options (data-* attributes):
 *   data-hotel-code       (required) Hotel code, e.g. "khil"
 *   data-api-url          (required) Kebo server URL, e.g. "https://kebo.kepsla.ai"
 *   data-phase            Booking phase: pre_booking | pre_checkin | during_stay | post_checkout (default: pre_booking)
 *   data-bot-name         Bot display name (default: "Kebo")
 *   data-brand-color      Hex color for header/buttons (default: "#FF6816")
 *   data-position         Widget position: "right" or "left" (default: "right")
 *   data-logo-url         URL of bot avatar image
 *   data-greeting         Custom welcome message
 *   data-widget-id        Unique widget identifier (default: hotel code)
 *   data-width            Widget width in px (default: 380)
 *   data-height           Widget height in px (default: 620)
 *   data-guest-id         Pre-fill guest ID
 *   data-entity-id        Pre-fill entity ID
 *   data-organisation-id  Pre-fill organisation ID
 *   data-room-number      Pre-fill room number
 *   data-guest-phone      Pre-fill guest phone
 *   data-guest-name       Pre-fill guest name
 */

(function () {
  "use strict";

  // Prevent double-loading
  if (window.__keboWidgetLoaded) return;
  window.__keboWidgetLoaded = true;

  // ── Read config from the <script> tag ──────────────────────────────────────
  var scriptTag = document.currentScript || (function () {
    var scripts = document.getElementsByTagName("script");
    return scripts[scripts.length - 1];
  })();

  function attr(name, fallback) {
    var val = scriptTag.getAttribute("data-" + name);
    return (val !== null && val !== "") ? val : (fallback || "");
  }

  var hotelCode   = attr("hotel-code");
  var apiUrl      = attr("api-url");
  var phase       = attr("phase", "pre_booking");
  // Brand color + bot name are resolved from the DB deployment theme (the single
  // source of truth) via /api/widget/bootstrap below. The data-* attributes are
  // only an override the host site sets explicitly; when absent we use the DB
  // value, falling back to these built-in defaults only if the fetch fails.
  var DEFAULT_BRAND = "C72C41";
  var DEFAULT_BOT   = "Kebo";
  var rawBotAttr     = scriptTag.getAttribute("data-bot-name");
  var rawBrandAttr   = scriptTag.getAttribute("data-brand-color");
  var botNameExplicit    = rawBotAttr !== null && rawBotAttr !== "";
  var brandColorExplicit = rawBrandAttr !== null && rawBrandAttr !== "";
  var botName     = botNameExplicit ? rawBotAttr : DEFAULT_BOT;
  var brandColor  = (brandColorExplicit ? rawBrandAttr : DEFAULT_BRAND).replace(/^#/, "");
  var position    = attr("position", "right");
  var logoUrl     = attr("logo-url");
  var greeting    = attr("greeting");
  var widgetId    = attr("widget-id", hotelCode);
  var width       = attr("width", "380");
  var height      = attr("height", "620");

  // Guest metadata (pre-fill)
  var guestId      = attr("guest-id");
  var entityId     = attr("entity-id");
  var orgId        = attr("organisation-id");
  var roomNumber   = attr("room-number");
  var guestPhone   = attr("guest-phone");
  var guestName    = attr("guest-name");

  if (!hotelCode) {
    console.warn("[Kebo Widget] data-hotel-code is required. Widget not loaded.");
    return;
  }
  if (!apiUrl) {
    console.warn("[Kebo Widget] data-api-url is required. Widget not loaded.");
    return;
  }

  // ── Mount: build the iframe + launcher using the resolved brand color/name ──
  function mount(brandColor, botName) {
  // ── Build the iframe src URL ───────────────────────────────────────────────
  var chatPath = "/chat";
  var base = apiUrl.replace(/\/$/, "");

  var params = [
    "embed=true",
    "hotel_code=" + encodeURIComponent(hotelCode),
    "api_url=" + encodeURIComponent(base),
    "phase=" + encodeURIComponent(phase),
    "bot_name=" + encodeURIComponent(botName),
    "brand_color=" + encodeURIComponent(brandColor),
    "position=" + encodeURIComponent(position),
    "widget_id=" + encodeURIComponent(widgetId),
    "width=" + encodeURIComponent(width),
    "height=" + encodeURIComponent(height),
  ];

  if (logoUrl)    params.push("logo_url="    + encodeURIComponent(logoUrl));
  if (greeting)   params.push("greeting="    + encodeURIComponent(greeting));
  if (guestId)    params.push("guest_id="    + encodeURIComponent(guestId));
  if (entityId)   params.push("entity_id="   + encodeURIComponent(entityId));
  if (orgId)      params.push("organisation_id=" + encodeURIComponent(orgId));
  if (roomNumber) params.push("room_number=" + encodeURIComponent(roomNumber));
  if (guestPhone) params.push("guest_phone=" + encodeURIComponent(guestPhone));
  if (guestName)  params.push("guest_name="  + encodeURIComponent(guestName));

  var iframeSrc = base + chatPath + "?" + params.join("&");

  // ── Inject styles ──────────────────────────────────────────────────────────
  var style = document.createElement("style");
  style.textContent = [
    "#kebo-widget-container {",
    "  position: fixed;",
    "  bottom: 20px;",
    "  " + (position === "left" ? "left" : "right") + ": 20px;",
    "  width: " + width + "px;",
    "  height: " + height + "px;",
    "  z-index: 2147483647;",
    "  border: none;",
    "  border-radius: 16px;",
    "  overflow: hidden;",
    "  box-shadow: 0 8px 32px rgba(0,0,0,0.18);",
    "  transition: opacity 0.2s ease, transform 0.2s ease;",
    "}",
    "#kebo-widget-container.kebo-hidden {",
    "  opacity: 0;",
    "  pointer-events: none;",
    "  transform: translateY(12px) scale(0.97);",
    "}",
    "#kebo-widget-toggle {",
    "  position: fixed;",
    "  bottom: 20px;",
    "  " + (position === "left" ? "left" : "right") + ": 20px;",
    "  width: 56px;",
    "  height: 56px;",
    "  border-radius: 50%;",
    "  background: #" + brandColor + ";",
    "  border: none;",
    "  cursor: pointer;",
    "  z-index: 2147483646;",
    "  box-shadow: 0 4px 16px rgba(0,0,0,0.22);",
    "  display: flex;",
    "  align-items: center;",
    "  justify-content: center;",
    "  transition: transform 0.2s ease;",
    "}",
    "#kebo-widget-toggle:hover { transform: scale(1.08); }",
    "#kebo-widget-toggle svg { width: 26px; height: 26px; fill: white; }",
  ].join("\n");
  document.head.appendChild(style);

  // ── Create the toggle button ───────────────────────────────────────────────
  var toggleBtn = document.createElement("button");
  toggleBtn.id = "kebo-widget-toggle";
  toggleBtn.setAttribute("aria-label", "Open " + botName + " chat");
  toggleBtn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M20 2H4a2 2 0 00-2 2v14a2 2 0 002 2h4l4 4 4-4h4a2 2 0 002-2V4a2 2 0 00-2-2z"/></svg>';
  document.body.appendChild(toggleBtn);

  // ── Create the iframe container ────────────────────────────────────────────
  var container = document.createElement("div");
  container.id = "kebo-widget-container";
  container.className = "kebo-hidden";

  var iframe = document.createElement("iframe");
  iframe.src = iframeSrc;
  iframe.style.cssText = "width:100%;height:100%;border:none;display:block;";
  iframe.setAttribute("allow", "microphone");
  iframe.setAttribute("title", botName + " Chat");

  container.appendChild(iframe);
  document.body.appendChild(container);

  // ── Toggle open/close ──────────────────────────────────────────────────────
  var isOpen = false;

  function openWidget() {
    isOpen = true;
    container.classList.remove("kebo-hidden");
    toggleBtn.setAttribute("aria-label", "Close " + botName + " chat");
    toggleBtn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>';
  }

  function closeWidget() {
    isOpen = false;
    container.classList.add("kebo-hidden");
    toggleBtn.setAttribute("aria-label", "Open " + botName + " chat");
    toggleBtn.innerHTML = '<svg viewBox="0 0 24 24"><path d="M20 2H4a2 2 0 00-2 2v14a2 2 0 002 2h4l4 4 4-4h4a2 2 0 002-2V4a2 2 0 00-2-2z"/></svg>';
  }

  toggleBtn.addEventListener("click", function () {
    isOpen ? closeWidget() : openWidget();
  });

  // Listen for close event from inside the iframe
  window.addEventListener("message", function (event) {
    if (!event.data || event.data.source !== "kebo-widget") return;
    if (event.data.type === "widget:close") closeWidget();
    if (event.data.type === "widget:open")  openWidget();
  });

  // ── Expose public API ──────────────────────────────────────────────────────
  window.KeboWidget = {
    open:  openWidget,
    close: closeWidget,
    toggle: function () { isOpen ? closeWidget() : openWidget(); },
  };
  } // end mount()

  // ── Resolve theme + bot name from the DB deployment, then mount ────────────
  // The DB (Admin > Widget / new_bot_widget_deployments) is the single source of
  // truth. Host data-brand-color / data-bot-name still win when explicitly set;
  // otherwise we use the DB values, falling back to the built-in defaults only if
  // the bootstrap fetch fails or times out (the launcher must never be blocked).
  function resolveAndMount() {
    if (brandColorExplicit && botNameExplicit) {
      mount(brandColor, botName);
      return;
    }
    var base = apiUrl.replace(/\/$/, "");
    var url = base + "/api/widget/bootstrap?hotel_code=" + encodeURIComponent(hotelCode);
    var done = false;
    function finish(dbBrand, dbBot) {
      if (done) return;
      done = true;
      var b = brandColorExplicit ? brandColor : (dbBrand ? String(dbBrand).replace(/^#/, "") : brandColor);
      var n = botNameExplicit ? botName : (dbBot ? String(dbBot) : botName);
      mount(b, n);
    }
    // Hard timeout so a slow/unreachable API never leaves the site without a launcher.
    var timer = setTimeout(function () { finish("", ""); }, 2500);
    try {
      fetch(url, { headers: { Accept: "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (cfg) {
          clearTimeout(timer);
          var theme = (cfg && cfg.theme) || {};
          finish(theme.brand_color || "", (cfg && cfg.bot_name) || "");
        })
        .catch(function () { clearTimeout(timer); finish("", ""); });
    } catch (e) {
      clearTimeout(timer);
      finish("", "");
    }
  }

  resolveAndMount();

})();
