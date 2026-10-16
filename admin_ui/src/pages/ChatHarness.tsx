import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { MessageCircle, Maximize2, Minus, Plus, Send, User, X } from "lucide-react";
import { useIsMobile } from "@/hooks/use-mobile";
import { useWebSocketChat } from "@/hooks/useWebSocketChat";
import { PhoneCountryPicker } from "@/components/PhoneCountryPicker";
import {
  DEFAULT_PHONE_COUNTRY,
  PHONE_MAX_DIGITS,
  clampPhoneDigits,
  formatPhonePreview,
  splitPhone,
  toE164,
  validatePhone,
  type PhoneCountryCode,
} from "@/lib/phone";

type Role = "user" | "assistant" | "agent" | "system";

const WIDGET_BRAND_COLOR_DEFAULT = "#FF6816";
const WIDGET_ACCENT_COLOR_DEFAULT = "#FF6816";
const WIDGET_BACKGROUND_COLOR_DEFAULT = "#FFFFFF";
const WIDGET_TEXT_COLOR_DEFAULT = "#1A1A2E";
const WIDGET_BOT_NAME_DEFAULT = "Kebo";
const WIDGET_DEFAULT_WIDTH = 380;
const WIDGET_DEFAULT_HEIGHT = 620;
const WIDGET_EXPANDED_WIDTH = 520;
const WIDGET_EXPANDED_HEIGHT = 760;

/**
 * One row of the opening lead form, as Admin > Forms stores it.
 * `fieldType` is the semantic identity the code keys off — the label is free
 * text an operator may rename at will, so nothing may depend on it.
 */
// Opening buttons an admin set in Bot Training -> Pre-booking. "message" sends
// its message as the guest's words; "intro" shows the saved message for the
// hotel's location (the hotel comparison, with one button per hotel).
type AdminWelcomeButton = { label: string; action: "message" | "intro"; message: string };
type PrebookingWelcomeConfig = { buttons: AdminWelcomeButton[]; after_lead_form: "intro" | "buttons" };

type LeadFieldType =
  | "FULL_NAME"
  | "EMAIL"
  | "MOBILE_NUMBER"
  | "ALTERNATE_MOBILE_NUMBER"
  | "CITY"
  | "CUSTOM";

interface LeadFormField {
  label: string;
  type?: string;
  fieldType: LeadFieldType;
  required?: boolean;
  placeholder?: string;
  options?: string[];
}

/** Exactly the fields this form hardcoded before it became configurable. */
const DEFAULT_LEAD_FORM_FIELDS: LeadFormField[] = [
  { label: "Full name", type: "text", fieldType: "FULL_NAME", required: true, placeholder: "Your full name" },
  { label: "Phone number", type: "tel", fieldType: "MOBILE_NUMBER", required: true, placeholder: "Phone number" },
  { label: "Email", type: "email", fieldType: "EMAIL", required: true, placeholder: "you@example.com" },
  { label: "Destination", type: "select", fieldType: "CITY", required: true, placeholder: "Choose the city you want to stay in…" },
];

interface PropertyOption {
  code: string;
  name: string;
  city: string;
}

interface PhaseOption {
  id: string;
  name: string;
}

interface ProfileMap {
  guest_id?: string;
  entity_id?: string;
  organisation_id?: string;
  room_number?: string;
  guest_phone?: string;
  guest_name?: string;
  guest_email?: string;
  check_in_date?: string;
  check_out_date?: string;
  group_id?: string;
  ticket_source?: string;
  flow?: string;
}

interface BookingRow {
  booking_id: number;
  guest_id?: number;
  confirmation_code?: string;
  property_name?: string;
  room_number?: string;
  room_type?: string;
  check_in_date?: string;
  check_out_date?: string;
  guest_name?: string;
  guest_phone?: string;
  status?: string;
  phase?: string;
}

type MessageStatus = "sending" | "sent" | "delivered" | "error";

interface ChatMessageRow {
  id: string;
  role: Role;
  content: string;
  canonicalContent: string;
  raw?: ChatApiResponse;
  status?: MessageStatus; // only set on user messages
  timestamp?: number;
  // Special inline UI rendered inside the bubble. "location_picker" renders a
  // location dropdown as a bot message (used to gate multi-property chats).
  kind?: "location_picker";
}

interface ChatApiResponse {
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

interface AssistFormState {
  messageId: string;
  propertyName: string;
  checkIn: string;
  checkOut: string;
  guests: string;
  message: string;
  errors: Record<string, string>;
  submitting: boolean;
}

interface ChatImageCard {
  id: string;
  title: string;
  url: string;
  description?: string;
  category?: string;
  // Sub-property name (new_bot_properties.name) for the image's scope. Shown
  // below the title when present, so a side-by-side compare of two properties
  // visually labels each image with the property it belongs to.
  propertyName?: string;
}

interface ChatDocumentCard {
  id: string;
  title: string;
  url: string;
  kind: string;
}

interface SuggestionsApiResponse {
  suggestions?: string[];
  prefetch_batch_id?: string | null;
}

interface InlineFormField {
  id: string;
  label: string;
  type: string;
  required: boolean;
  options: string[];
  placeholder: string;
  defaultValue: string;
  prefillSource: string;
  // Deterministic time-slot constraints (Sarovar-scoped, driven by per-field
  // config — no hotel names hardcoded in this component). When a time field
  // carries these, it renders an easy slot dropdown instead of a native time
  // input, and can filter the slots by the value of another field (e.g. outlet).
  slotSourceField?: string;
  slotHoursMap?: Record<string, [string, string]>;
  slotHours?: [string, string];
  slotStepMinutes?: number;
}

interface InlineFormState {
  messageId: string;
  serviceId: string;
  fields: InlineFormField[];
  values: Record<string, string>;
  countryCodes: Record<string, PhoneCountryCode>;
  errors: Record<string, string>;
  submitting: boolean;
  successMessage: string;
  // Server-driven (metadata.form_cancellable): only hotels opted into
  // CHAT_FORM_CANCEL_ENABLED_HOTELS_JSON get the "Cancel request" button, so the
  // widget itself stays hotel-agnostic.
  cancellable: boolean;
  cancelling: boolean;
}

interface LocationManifestEntry {
  label: string;
  property_names: string[];
}

interface TicketStatus {
  label: string;
  badge: "idle" | "created" | "not-created" | "pending" | "failed";
}

interface SessionHistory {
  session_id: string;
  state: string;
  hotel_code: string;
  created_at: string;
  messages: Array<{ role: string; content: string; timestamp: string }>;
}

type InteractionMeta = {
  source_type?: string;
  source_label?: string;
  source_text?: string;
};

interface TicketRow {
  ticket_id?: string;
  id?: string;
  status?: string;
  created_at?: string;
  [key: string]: unknown;
}

const PHASE_FALLBACK: PhaseOption[] = [
  { id: "pre_booking", name: "Pre Booking" },
  { id: "pre_checkin", name: "Pre Checkin" },
  { id: "during_stay", name: "During Stay" },
  { id: "post_checkout", name: "Post Checkout" },
];

function generateSessionId() {
  return `session_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
}

// A conversation id identifies ONE chat thread. It rotates every time a new
// chat is started (e.g. the dustbin / "new chat" action), while the session id
// stays the same — so one visitor (session) can own many conversations.
function newConversationId() {
  return `${Date.now().toString(16)}-${Math.random().toString(16).slice(2, 10)}-${Math.random().toString(16).slice(2, 10)}`;
}

// The session id identifies the VISITOR and must stay stable across reloads,
// new tabs and message-blob expiry. It lives in its own dedicated localStorage
// key (kebo's SessionUtil pattern), separate from the per-conversation blob, so
// clearing a chat never throws away the visitor identity.
function sessionIdStorageKey(widgetId?: string, hotelCode?: string) {
  return `kebo_sid_${widgetId || hotelCode || "default"}`;
}
function readStoredSessionId(key: string): string {
  try {
    const v = (localStorage.getItem(key) || "").trim();
    return v && v.toLowerCase() !== "null" && v.toLowerCase() !== "undefined" ? v : "";
  } catch {
    return "";
  }
}
function writeStoredSessionId(key: string, id: string) {
  try {
    if (id) localStorage.setItem(key, id);
  } catch {
    /* storage unavailable — ignore */
  }
}

function normalizePhaseId(value: unknown): string {
  const raw = String(value || "")
    .trim()
    .toLowerCase()
    .replace(/-/g, "_")
    .replace(/\s+/g, "_");
  if (!raw) return "";
  const aliases: Record<string, string> = {
    prebooking: "pre_booking",
    booking: "pre_checkin",
    precheckin: "pre_checkin",
    duringstay: "during_stay",
    instay: "during_stay",
    in_stay: "during_stay",
    postcheckout: "post_checkout",
  };
  return aliases[raw] || raw;
}

function normalizeBookingLeadTurnText(value: unknown): string {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/\s+/g, " ");
}

function looksLikeBookingLeadSlotValue(text: string): boolean {
  if (!text) return false;
  if (/[^\s@]+@[^\s@]+\.[^\s@]+/.test(text)) return true;
  if ((text.match(/\d/g) || []).length >= 7) return true;
  if (/\b\d{4}-\d{1,2}-\d{1,2}\b/.test(text)) return true;
  if (/\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b/.test(text)) return true;
  if (/\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b/i.test(text)) return true;
  return [
    "check in",
    "check-in",
    "check out",
    "check-out",
    "checkout",
    "guest",
    "guests",
    "adult",
    "adults",
    "child",
    "children",
    "night",
    "nights",
    "full name",
    "contact number",
  ].some((marker) => text.includes(marker));
}

function shouldSendBookingLeadResumeMetadata(leadId: string, interactionMeta?: InteractionMeta): boolean {
  if (!String(leadId || "").trim()) return false;
  const sourceType = normalizeBookingLeadTurnText(interactionMeta?.source_type);
  if (sourceType === "cta_book_stay" || sourceType === "cta_location_picker") return true;
  const text = normalizeBookingLeadTurnText(interactionMeta?.source_text || interactionMeta?.source_label || "");
  if (!text) return false;
  if (["yes", "yes confirm", "confirm", "ok", "okay", "sure", "proceed", "continue"].includes(text)) {
    return true;
  }
  if (
    /\b(?:i\s+(?:want|wanna|need|would\s+like)\s+to\s+book|book\s+(?:it|this|that|now|me|my)|help\s+me\s+book|let'?s\s+book|reserve|reservation|go\s+ahead|proceed|continue|complete\s+(?:the|my)\s+booking|confirm)\b/i.test(
      text,
    )
  ) {
    return true;
  }
  return looksLikeBookingLeadSlotValue(text);
}

function resolveAssistantDisplayMessage(data: ChatApiResponse): string {
  const display = String(data.display_message || "").trim();
  if (display) return display;
  const metadataDisplay = String((data.metadata || {}).display_message || "").trim();
  if (metadataDisplay) return metadataDisplay;
  return String(data.message || "").trim();
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function normalizeSafeLink(url: string): string {
  const raw = String(url || "").trim();
  if (!raw) return "";
  const withProtocol = /^https?:\/\//i.test(raw) ? raw : `https://${raw}`;
  try {
    const parsed = new URL(withProtocol);
    if (!["http:", "https:"].includes(parsed.protocol)) return "";
    return parsed.href;
  } catch {
    return "";
  }
}

function buildAssistantLinkHtml(url: string, label: string): string {
  const href = normalizeSafeLink(url);
  const text = String(label || url || "").trim();
  if (!href || !text) return escapeHtml(text || url || "");
  return `<a class="underline underline-offset-2 text-teal-700 hover:text-teal-800" href="${escapeHtml(
    href,
  )}" target="_blank" rel="noopener noreferrer">${escapeHtml(text)}</a>`;
}

function normalizeChatImages(raw: unknown): ChatImageCard[] {
  if (!Array.isArray(raw)) return [];
  const parsed: ChatImageCard[] = [];
  const seen = new Set<string>();
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const row = item as Record<string, unknown>;
    const url = normalizeSafeLink(String(row.url || row.image_url || row.image || "").trim());
    const title = String(row.title || row.label || row.name || "").trim();
    if (!url || !title) continue;
    const dedupeKey = `${title.toLowerCase()}|${url.toLowerCase()}`;
    if (seen.has(dedupeKey)) continue;
    seen.add(dedupeKey);
    parsed.push({
      id: String(row.id || dedupeKey),
      title,
      url,
      description: String(row.description || row.caption || row.text || row.summary || "").trim(),
      category: String(row.category || "").trim(),
      propertyName: String(row.property_name || "").trim() || undefined,
    });
  }
  return parsed;
}

// Resolve a document URL against the backend origin. Menu URLs come from the
// backend as root-relative paths (/static/menus/...); the chat UI may be embedded
// on the hotel's domain, so we anchor them to the backend that hosts the file.
// Absolute http(s) URLs pass through unchanged; anything else is rejected.
function resolveDocumentUrl(url: string, apiBase: string): string {
  const raw = String(url || "").trim();
  if (!raw) return "";
  if (/^https?:\/\//i.test(raw)) return raw;
  if (raw.startsWith("/")) {
    const base = String(apiBase || "").replace(/\/$/, "");
    return `${base}${raw}`;
  }
  return "";
}

function normalizeChatDocuments(raw: unknown): ChatDocumentCard[] {
  if (!Array.isArray(raw)) return [];
  const parsed: ChatDocumentCard[] = [];
  const seen = new Set<string>();
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const row = item as Record<string, unknown>;
    const url = String(row.url || "").trim();
    const title = String(row.title || row.label || row.name || "").trim();
    if (!url || !title) continue;
    const dedupeKey = url.toLowerCase();
    if (seen.has(dedupeKey)) continue;
    seen.add(dedupeKey);
    parsed.push({
      id: String(row.id || dedupeKey),
      title,
      url,
      kind: String(row.kind || "pdf").trim().toLowerCase() || "pdf",
    });
  }
  return parsed;
}

function AssistantDocumentCards({
  documents,
  apiBase,
  accentColor,
}: {
  documents: ChatDocumentCard[];
  apiBase: string;
  accentColor: string;
}) {
  if (!documents.length) return null;
  return (
    <div className="mt-2 flex flex-col gap-2">
      {documents.map((doc) => {
        const href = resolveDocumentUrl(doc.url, apiBase);
        if (!href) return null;
        return (
          <a
            key={doc.id}
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            title={`Open ${doc.title}`}
            className="group flex items-center gap-3 rounded-xl border border-slate-200 bg-white p-2.5 no-underline shadow-sm transition hover:border-slate-300 hover:shadow-md"
          >
            <span
              className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg text-white"
              style={{ backgroundColor: accentColor }}
              aria-hidden="true"
            >
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                <path d="M14 2v6h6" />
              </svg>
            </span>
            <span className="min-w-0 flex-1">
              <span className="block text-sm font-semibold leading-snug text-slate-900">{doc.title}</span>
              <span className="mt-0.5 block text-xs text-slate-500">{doc.kind.toUpperCase()} · tap to open</span>
            </span>
            <span
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-white transition group-hover:brightness-110"
              style={{ backgroundColor: accentColor }}
              aria-hidden="true"
            >
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                <polyline points="7 10 12 15 17 10" />
                <line x1="12" y1="15" x2="12" y2="3" />
              </svg>
            </span>
          </a>
        );
      })}
    </div>
  );
}

function AssistantImageCarousel({ images, accentColor }: { images: ChatImageCard[]; accentColor: string }) {
  const [index, setIndex] = useState(0);
  const [viewerOpen, setViewerOpen] = useState(false);
  // An image the browser already has (Cloudinary serves them immutable for 30
  // days) can finish loading BEFORE React attaches onLoad - the event then never
  // fires for that element and the "Loading image..." overlay sits on top of a
  // picture that is already there, for good. That is why images seen in an
  // earlier chat looked like they were loading forever after a refresh. These
  // refs let the <img> report itself complete on mount.
  const inlineImgRef = useRef<HTMLImageElement | null>(null);
  const viewerImgRef = useRef<HTMLImageElement | null>(null);
  const [inlineLoading, setInlineLoading] = useState(true);
  const [inlineFailed, setInlineFailed] = useState(false);
  const [viewerLoading, setViewerLoading] = useState(true);
  const [viewerFailed, setViewerFailed] = useState(false);

  useEffect(() => {
    setIndex(0);
    setViewerOpen(false);
  }, [images]);

  const totalImages = images.length;
  const safeIndex = totalImages > 0 ? Math.max(0, Math.min(index, totalImages - 1)) : 0;
  const current = images[safeIndex];

  useEffect(() => {
    // Start from "already loaded?" rather than assuming a fresh download.
    const inlineDone = inlineImgRef.current?.complete === true;
    const viewerDone = viewerImgRef.current?.complete === true;
    setInlineLoading(!inlineDone);
    setInlineFailed(false);
    setViewerLoading(!viewerDone);
    setViewerFailed(false);
  }, [safeIndex, current?.url]);

  useEffect(() => {
    if (!totalImages || !current?.url) return;
    const neighborIndexes = [safeIndex - 1, safeIndex + 1].filter((value) => value >= 0 && value < totalImages);
    for (const idx of neighborIndexes) {
      const preload = new Image();
      preload.src = images[idx].url;
    }
  }, [images, safeIndex, totalImages, current?.url]);

  if (!totalImages || !current) return null;

  return (
    <div className="mt-2 rounded-xl border border-slate-200 bg-slate-50 p-2">
      <button
        type="button"
        onClick={() => setViewerOpen(true)}
        className="relative block w-full overflow-hidden rounded-lg border border-slate-200 bg-white text-left"
      >
        {inlineLoading ? (
          <div className="absolute inset-0 flex items-center justify-center bg-slate-100 text-[11px] text-slate-500">
            Loading image...
          </div>
        ) : null}
        {inlineFailed ? (
          <div className="flex h-36 w-full items-center justify-center bg-slate-100 text-xs text-slate-500">
            Image failed to load
          </div>
        ) : (
          <img
            ref={(node) => {
              inlineImgRef.current = node;
              // A cached image is already complete the moment it mounts.
              if (node?.complete) setInlineLoading(false);
            }}
            src={current.url}
            alt={current.title}
            loading={safeIndex <= 1 ? "eager" : "lazy"}
            className="h-36 w-full object-cover"
            onLoad={() => setInlineLoading(false)}
            onError={() => {
              setInlineLoading(false);
              setInlineFailed(true);
            }}
          />
        )}
      </button>
      <div className="mt-2 flex items-center justify-between gap-2 text-xs">
        {/* Only a SINGLE image hides the pair - there is nowhere to go. With two
            or more they stay exactly as they were, greying out at each end. */}
        {totalImages > 1 ? (
          <button
            type="button"
            disabled={safeIndex === 0}
            onClick={() => setIndex((prev) => Math.max(0, prev - 1))}
            className="rounded-md border border-slate-300 bg-white px-2 py-1 text-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Prev
          </button>
        ) : null}
        <div className="min-w-0 flex-1 text-center">
          <p className="truncate font-medium text-slate-800">{current.title}</p>
          {current.propertyName ? (
            <p className="mx-auto mt-0.5 max-w-[92%] line-clamp-2 text-[11px] leading-snug text-slate-700">
              {current.propertyName}
            </p>
          ) : current.description ? (
            <p className="mx-auto mt-0.5 max-w-[92%] line-clamp-2 text-[11px] leading-snug text-slate-600">
              {current.description}
            </p>
          ) : null}
          <p className="text-[11px] text-slate-500">
            {safeIndex + 1}/{totalImages}
            {current.category ? ` | ${current.category}` : ""}
          </p>
        </div>
        {totalImages > 1 ? (
          <button
            type="button"
            disabled={safeIndex >= totalImages - 1}
            onClick={() => setIndex((prev) => Math.min(totalImages - 1, prev + 1))}
            className="rounded-md border border-slate-300 bg-white px-2 py-1 text-slate-700 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Next
          </button>
        ) : null}
      </div>

      {viewerOpen ? (
        <div className="fixed inset-0 z-[90] bg-black/90 p-3 sm:p-5">
          <div className="mx-auto flex h-full w-full max-w-5xl flex-col">
            <div className="mb-3 flex items-center justify-between">
              <button
                type="button"
                onClick={() => setViewerOpen(false)}
                className="rounded-md border border-white/30 bg-white/10 px-3 py-1 text-xs text-white"
                style={{ borderColor: `${accentColor}90` }}
              >
                Back to chat
              </button>
              <p className="text-xs text-white">
                {safeIndex + 1}/{totalImages}
              </p>
            </div>

            <div className="relative flex min-h-0 flex-1 items-center justify-center rounded-xl bg-black/70 p-3">
              {totalImages > 1 ? (
                <button
                  type="button"
                  disabled={safeIndex === 0}
                  onClick={() => setIndex((prev) => Math.max(0, prev - 1))}
                  className="absolute left-2 rounded-full border border-white/30 bg-black/60 px-3 py-2 text-sm text-white disabled:opacity-30"
                  aria-label="Previous image"
                >
                  {"<"}
                </button>
              ) : null}

              {viewerLoading ? (
                <div className="absolute inset-0 flex items-center justify-center text-sm text-white/80">
                  Loading image...
                </div>
              ) : null}
              {viewerFailed ? (
                <div className="flex h-full w-full items-center justify-center text-sm text-white/80">
                  Image failed to load
                </div>
              ) : (
                <img
                  ref={(node) => {
                    viewerImgRef.current = node;
                    if (node?.complete) setViewerLoading(false);
                  }}
                  src={current.url}
                  alt={current.title}
                  className="max-h-full max-w-full object-contain"
                  loading="eager"
                  onLoad={() => setViewerLoading(false)}
                  onError={() => {
                    setViewerLoading(false);
                    setViewerFailed(true);
                  }}
                />
              )}

              {totalImages > 1 ? (
                <button
                  type="button"
                  disabled={safeIndex >= totalImages - 1}
                  onClick={() => setIndex((prev) => Math.min(totalImages - 1, prev + 1))}
                  className="absolute right-2 rounded-full border border-white/30 bg-black/60 px-3 py-2 text-sm text-white disabled:opacity-30"
                  aria-label="Next image"
                >
                  {">"}
                </button>
              ) : null}
            </div>

            <div className="mt-3 text-center text-white">
              <p className="text-sm font-medium">{current.title}</p>
              {current.description ? <p className="mt-1 text-xs text-white/85">{current.description}</p> : null}
              {current.category ? <p className="mt-1 text-[11px] text-white/70">{current.category}</p> : null}
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

// Assistant bubbles longer than this (raw character count) collapse behind a
// "Read more" toggle. ~520 chars ≈ 9–11 lines at the widget's width — long
// canned intros and verbose LLM answers clamp; ordinary replies never do.
const LONG_ASSISTANT_MESSAGE_CHARS = 520;

// gpt-5.4 mangles astral emojis when it answers in JSON mode: an emoji like
// 🍴 (U+1F374) arrives as a control char (U+0001–U+001F) followed by its low 3
// hex digits ("374"). Reconstruct cp = (controlChar << 12) | hex; if that lands
// in the emoji plane, restore the emoji, otherwise drop the stray control char.
function repairMangledEmoji(text: string): string {
  return String(text || "").replace(/[\u0001-\u001f]([0-9a-fA-F]{3})/g, (_m, hex: string) => {
    const cp = (_m.charCodeAt(0) << 12) | parseInt(hex, 16);
    if (cp >= 0x1f000 && cp <= 0x1faff) {
      try {
        return String.fromCodePoint(cp);
      } catch {
        return "";
      }
    }
    return hex; // not an emoji reconstruction — keep the text, drop the control char
  });
}

// Wrap *emphasis* / _emphasis_ runs in <em>.
//
// This deliberately does NOT use a lookbehind. `(?<!\w)` is a SyntaxError in
// Safari below 16.4, and an unsupported regex literal fails at PARSE time — so
// the two lines that used to live here took the ENTIRE chat bundle down on
// older iPhones. The widget painted but nothing worked: no typing, no taps, no
// form. Same rule, expressed as an explicit scan: the opening delimiter must
// not sit directly after a word character, and the run may not begin or end
// with whitespace. On a rejected delimiter we resume from the very next
// character rather than past the whole attempted span, so a run starting inside
// it is still found — exactly what the lookbehind did.
function applyEmphasis(source: string, delimiter: "*" | "_"): string {
  const pattern =
    delimiter === "*"
      ? /\*([^*\s]|[^*\s][^*]*[^*\s])\*(?!\w)/g
      : /_([^_\s]|[^_\s][^_]*[^_\s])_(?!\w)/g;
  let out = "";
  let last = 0;
  let match: RegExpExecArray | null;
  pattern.lastIndex = 0;
  while ((match = pattern.exec(source)) !== null) {
    const start = match.index;
    if (start > 0 && /\w/.test(source.charAt(start - 1))) {
      pattern.lastIndex = start + 1;
      continue;
    }
    out += source.slice(last, start) + `<em>${match[1]}</em>`;
    last = start + match[0].length;
  }
  return out + source.slice(last);
}

// Phone numbers the bot prints ("call the hotel on 044-71816000/01 or
// 8939992709") are a dead end on a phone: the guest has to memorise them and
// retype them into the dialler. They become tap-to-call links here.
//
// Runs LAST, on the already-escaped HTML, and only on the text BETWEEN tags -
// never inside an attribute, and never inside an <a> we just built, so a booking
// URL with digits in it is left alone.
//
// What counts as a number to dial: 8-15 digits, optionally with +, spaces or
// hyphens. Deliberately NOT matched - anything with a colon (5:00 AM), a comma
// (1,500 sq ft) or the shape of a date (2026-09-16). A trailing "/01" (a second
// line sharing the prefix) stays as plain text next to the linked number.
const PHONE_CANDIDATE = /\+?\d[\d\s-]{6,}\d/g;

function telHrefFor(raw: string): string {
  const trimmed = String(raw || "").trim();
  const digits = trimmed.replace(/\D/g, "");
  if (digits.length < 8 || digits.length > 15) return "";
  if (/[:,]/.test(trimmed)) return "";
  if (/^\d{4}-\d{2}-\d{2}$/.test(trimmed)) return "";
  return `tel:${trimmed.startsWith("+") ? "+" : ""}${digits}`;
}

function linkifyPhoneNumbers(html: string): string {
  const parts = String(html || "").split(/(<[^>]+>)/);
  let insideAnchor = false;
  return parts
    .map((part) => {
      if (part.startsWith("<")) {
        const tag = part.toLowerCase();
        if (tag.startsWith("<a")) insideAnchor = true;
        else if (tag.startsWith("</a")) insideAnchor = false;
        return part;
      }
      if (insideAnchor || !part) return part;
      return part.replace(PHONE_CANDIDATE, (match) => {
        const lead = match.match(/^\s+/)?.[0] || "";
        const tail = match.match(/[\s-]+$/)?.[0] || "";
        const core = match.slice(lead.length, match.length - tail.length);
        const href = telHrefFor(core);
        if (!href) return match;
        return `${lead}<a class="underline underline-offset-2" href="${href}">${core}</a>${tail}`;
      });
    })
    .join("");
}

function renderAssistantMarkdownToHtml(text: string): string {
  const escaped = escapeHtml(repairMangledEmoji(String(text || "")));
  if (!escaped) return "";
  let rendered = escaped;
  rendered = rendered.replace(/\*\*([\s\S]+?)\*\*/g, "<strong>$1</strong>");
  rendered = rendered.replace(/__([\s\S]+?)__/g, "<strong>$1</strong>");
  // NO LOOKBEHIND HERE. `(?<!...)` is a SyntaxError in Safari below 16.4, and an
  // unsupported regex literal fails at PARSE time — so one of these two lines
  // took the ENTIRE chat bundle down on older iPhones. The widget rendered but
  // nothing was interactive: no typing, no taps, no form. Same rule expressed by
  // capturing the preceding character instead: the delimiter must not follow a
  // word char, and the emphasised run may not begin or end with whitespace.
  rendered = applyEmphasis(rendered, "*");
  rendered = applyEmphasis(rendered, "_");
  rendered = rendered.replace(/^#{1,3}\s+(.+)$/gm, "<strong>$1</strong>");
  rendered = rendered.replace(/\[([^\]\n]+?)\]\((https?:\/\/[^\s)]+)\)/g, (_match, label, url) =>
    buildAssistantLinkHtml(url, label),
  );
  rendered = rendered.replace(/(^|[\s(>])((?:https?:\/\/|www\.)[^\s<]+)/gi, (_match, prefix, url) => {
    const trailing = (url.match(/[).,!?:;]+$/) || [""])[0];
    const cleanUrl = trailing ? url.slice(0, -trailing.length) : url;
    const linked = buildAssistantLinkHtml(cleanUrl, cleanUrl);
    return `${prefix}${linked}${trailing}`;
  });
  rendered = linkifyPhoneNumbers(rendered);
  return rendered;
}

function formatWelcomeMessage(
  template: string,
  context: { bot_name: string; business_name: string; city: string; guest_name?: string },
  isMultiProperty = false,
  phase = "",
): string {
  const fallback = "Hi! How can I help you today?";
  const raw = String(template || "").trim() || fallback;
  let out = raw
    .replace(/\{bot_name\}/gi, context.bot_name || "Assistant")
    .replace(/\{business_name\}/gi, context.business_name || "your property")
    .replace(/\{city\}/gi, context.city || "");

  // Title-case every word of the name ("sana sana" -> "Sana Sana").
  const guest = String(context.guest_name || "").trim();
  const pretty = guest
    .split(/\s+/)
    .map((w) => (w ? w.charAt(0).toUpperCase() + w.slice(1) : w))
    .join(" ");

  // Multi-property hotels (e.g. Zone): ALWAYS a short, clean greeting that asks
  // the guest to pick the destination they're planning their stay at — for both
  // a brand-new visitor ("Hi there!") and a returning (frozen-lead) guest
  // ("Hi {Name}!"). We never render the long auto-generated "here are all our
  // locations + property names" dump (the backend's multi_property_welcome_message).
  if (isMultiProperty) {
    // A returning guest (we already have their name on file) is greeted by name
    // and welcomed back — this is the bubble they see instead of the intro form,
    // including on a fresh conversation id. A brand-new visitor gets the intro
    // form instead, which carries its own greeting.
    if (pretty) {
      return `Hi ${pretty}, welcome back! Please choose the location you're planning your stay at to continue.`;
    }
    return `Hi there! How can I assist you today? Please choose the location you're planning your stay at to continue.`;
  }

  // SINGLE-property: a single hotel has no location to choose — never render the
  // multi-property "choose the location" line here. Greeting is phase-aware.
  const isPreCheckin =
    String(phase || "").toLowerCase().replace(/[-\s]+/g, "_") === "pre_checkin";
  if (isPreCheckin) {
    // Guest has booked and is arriving soon — warm, arrival-focused greeting.
    return guest
      ? `Hi ${pretty}, we're excited to have you! Is there anything I can help you with before your arrival?`
      : `Welcome! We're excited to have you. Is there anything I can help you with before your arrival?`;
  }
  const isDuringStay =
    String(phase || "").toLowerCase().replace(/[-\s]+/g, "_") === "during_stay";
  if (isDuringStay) {
    // In-house guest — action-oriented in-stay greeting.
    const who = pretty ? `Hi ${pretty}!` : "Hi!";
    const where = context.business_name || "the hotel";
    return `${who} 👋 Welcome — I'm your in-stay assistant at ${where}. Need fresh towels, a table reserved, something fixed, or the spa? Tap an option below or just ask.`;
  }
  if (guest) {
    // Single-property returning guest (frozen lead): clean greeting, no location choice.
    return `Hi ${pretty}! How can I assist you today?`;
  }
  out = out.replace(/\{guest_name\}/gi, "").replace(/\s{2,}/g, " ").trim();
  return out.trim();
}

function normalizeFormFields(raw: unknown): InlineFormField[] {
  if (!Array.isArray(raw)) return [];
  const rows: InlineFormField[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const row = item as Record<string, unknown>;
    const id = String(row.id || "").trim();
    const label = String(row.label || id || "").trim();
    if (!id || !label) continue;
    const options = Array.isArray(row.options)
      ? row.options.map((entry) => String(entry || "").trim()).filter(Boolean)
      : Array.isArray(row.choices)
        ? row.choices.map((entry) => String(entry || "").trim()).filter(Boolean)
        : [];
    const rawDefault = row.default_value;
    const defaultValue =
      typeof rawDefault === "string"
        ? rawDefault
        : rawDefault == null
          ? ""
          : String(rawDefault);
    const prefillSource = String(row.prefill_source || "").trim();
    // Optional per-field time-slot constraints (all optional; absent → native input).
    const slotSourceField = String(row.slot_source_field || "").trim() || undefined;
    const rawHoursMap = row.slot_hours_map;
    let slotHoursMap: Record<string, [string, string]> | undefined;
    if (rawHoursMap && typeof rawHoursMap === "object" && !Array.isArray(rawHoursMap)) {
      slotHoursMap = {};
      for (const [k, v] of Object.entries(rawHoursMap as Record<string, unknown>)) {
        if (Array.isArray(v) && v.length === 2) {
          slotHoursMap[k] = [String(v[0]), String(v[1])];
        }
      }
      if (Object.keys(slotHoursMap).length === 0) slotHoursMap = undefined;
    }
    const rawHours = row.slot_hours;
    const slotHours: [string, string] | undefined =
      Array.isArray(rawHours) && rawHours.length === 2
        ? [String(rawHours[0]), String(rawHours[1])]
        : undefined;
    const stepNum = Number(row.slot_step_minutes);
    const slotStepMinutes = Number.isFinite(stepNum) && stepNum > 0 ? stepNum : undefined;
    rows.push({
      id,
      label,
      type: String(row.type || "text").trim().toLowerCase().replace(/_/g, "-") || "text",
      required: Boolean(row.required),
      options,
      placeholder: String(row.placeholder || "").trim(),
      defaultValue,
      prefillSource,
      slotSourceField,
      slotHoursMap,
      slotHours,
      slotStepMinutes,
    });
  }
  return rows;
}

function isPhoneField(field: InlineFormField): boolean {
  if (field.type === "tel") return true;
  const hint = `${field.id} ${field.label}`.toLowerCase();
  return /\b(phone|mobile|contact|cell|whatsapp)\b/.test(hint);
}

// True for person-name fields (Name / Full Name / Guest Name). Excludes
// non-person "name" fields that legitimately contain digits or codes
// (property/hotel/company/room/username), so we don't reject valid input there.
function isNameField(field: InlineFormField): boolean {
  if (field.type && field.type !== "text") return false;
  const hint = `${field.id} ${field.label}`.toLowerCase();
  if (/(user|property|hotel|company|business|brand|room|file|booking)\s*name/.test(hint)) return false;
  return /\b(full[\s_-]?name|guest[\s_-]?name|your[\s_-]?name|name)\b/.test(hint);
}

// A person name must contain at least one letter and no digits. Allows Unicode
// letters (international guests), spaces, and . ' - (e.g. "Mary-Jane O'Neil",
// "José", "李明"). Returns an error string, or null when valid/empty (emptiness
// is handled by the required-field check).
function validateNameInput(rawValue: string): string | null {
  const value = (rawValue || "").trim();
  if (!value) return null;
  if (/\d/.test(value)) return "Name cannot contain numbers.";
  if (!/\p{L}/u.test(value)) return "Please enter a valid name.";
  return null;
}

function buildInlineRequiredFieldError(field: InlineFormField): string {
  const label = String(field.label || field.id || "this field").trim();
  if (field.type === "select" || field.type === "dropdown" || field.options.length > 0) {
    return `Please select ${label}.`;
  }
  return `Please enter ${label}.`;
}

function getStayDateBounds(phase: string, booking: BookingRow | null): { min: string; max: string } {
  const todayStr = new Date().toISOString().slice(0, 10);
  const normalized = normalizePhaseId(phase);
  const checkIn = String(booking?.check_in_date || "");
  const checkOut = String(booking?.check_out_date || "");
  if (normalized === "pre_checkin" && checkIn && checkOut) {
    const min = checkIn > todayStr ? checkIn : todayStr;
    return { min, max: checkOut };
  }
  if (normalized === "during_stay" && checkOut) {
    return { min: todayStr, max: checkOut };
  }
  return { min: todayStr, max: "" };
}

// Validate a picked date against the allowed [min, max] window. Returns an
// error string the moment the guest selects an out-of-range date, instead of
// waiting for submit. `<input type="date">` values are "YYYY-MM-DD", so plain
// string comparison is correct here (lexical order == chronological order).
function validateStayDate(value: string, bounds: { min: string; max: string }): string | null {
  const picked = String(value || "").trim();
  if (!picked) return null; // empty handled by the required-field check at submit
  if (bounds.min && picked < bounds.min) {
    return `Please pick a date on or after ${bounds.min}.`;
  }
  if (bounds.max && picked > bounds.max) {
    return `Please pick a date on or before ${bounds.max}.`;
  }
  return null;
}

// Parse "HH:MM" (24h) into minutes-since-midnight, or null if malformed.
function parseHHMM(value: string): number | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(value || "").trim());
  if (!m) return null;
  const h = Number(m[1]);
  const min = Number(m[2]);
  if (h < 0 || h > 23 || min < 0 || min > 59) return null;
  return h * 60 + min;
}

// Render minutes-since-midnight as a friendly 12h label, e.g. 390 → "6:30 AM".
function formatSlotLabel(minutes: number): string {
  const h24 = Math.floor(minutes / 60);
  const min = minutes % 60;
  const ampm = h24 < 12 ? "AM" : "PM";
  const h12 = h24 % 12 === 0 ? 12 : h24 % 12;
  return `${h12}:${String(min).padStart(2, "0")} ${ampm}`;
}

// Build discrete time slots between [start, end] (inclusive) stepping by
// `stepMinutes`. Returns { value: "HH:MM", label: "6:30 AM" }[]. Empty on bad input.
function generateTimeSlots(
  start: string,
  end: string,
  stepMinutes: number,
): { value: string; label: string }[] {
  const startMin = parseHHMM(start);
  const endMin = parseHHMM(end);
  const step = stepMinutes > 0 ? stepMinutes : 30;
  if (startMin == null || endMin == null || endMin < startMin) return [];
  const slots: { value: string; label: string }[] = [];
  for (let t = startMin; t <= endMin; t += step) {
    const hh = String(Math.floor(t / 60)).padStart(2, "0");
    const mm = String(t % 60).padStart(2, "0");
    slots.push({ value: `${hh}:${mm}`, label: formatSlotLabel(t) });
  }
  return slots;
}

interface EmbedRuntimeConfig {
  embedMode: boolean;
  widgetId: string;
  /**
   * Public deployment key — present ONLY when this page is a customer embed
   * (the loader puts it in the iframe URL). Forwarded to the server, which uses
   * it to apply that deployment's pinned model. Empty in the admin harness,
   * which is what leaves the harness model dropdown free to switch models
   * without touching any live customer bot.
   */
  widgetKey: string;
  hotelCode: string;
  phase: string;
  sessionId: string;
  brandColor: string;
  accentColor: string;
  backgroundColor: string;
  textColor: string;
  /** True when the host pinned brand_color via URL param (embed loader always does). */
  brandColorExplicit: boolean;
  botName: string;
  botNameExplicit: boolean;
  position: "left" | "right";
  width: number;
  height: number;
  metadataOverrides: ProfileMap;
  extraMetadata: Record<string, unknown>;
  disablePrefetch: boolean;
  /** Base URL of the Kebo server — required when widget is on a third-party site */
  apiUrl: string;
  logoUrl: string;
  greeting: string;
  sessionTtlHours: number;
  idleTimeoutMinutes: number;
}

function parseBooleanParam(value: string | null, fallback = false): boolean {
  const raw = String(value || "")
    .trim()
    .toLowerCase();
  if (!raw) return fallback;
  if (["1", "true", "yes", "on"].includes(raw)) return true;
  if (["0", "false", "no", "off"].includes(raw)) return false;
  return fallback;
}

function parseNumberParam(value: string | null, fallback: number, min: number, max: number): number {
  // An absent/empty param must use the fallback — NOT 0. (Number(null) and
  // Number("") both coerce to 0, which is finite, so without this guard a
  // missing param would silently clamp to `min` / disable a feature.)
  if (value === null || String(value).trim() === "") return fallback;
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, Math.round(parsed)));
}

function normalizeColorParam(raw: string, fallback: string): string {
  const value = String(raw || "").trim();
  if (!value) return fallback;
  const withHash = value.startsWith("#") ? value : `#${value}`;
  return /^#[0-9a-fA-F]{3,8}$/.test(withHash) ? withHash : fallback;
}

function readEmbedRuntimeConfig(): EmbedRuntimeConfig {
  if (typeof window === "undefined") {
    return {
      embedMode: false,
      widgetId: "default",
      widgetKey: "",
      hotelCode: "",
      phase: "pre_booking",
      sessionId: generateSessionId(),
      brandColor: WIDGET_BRAND_COLOR_DEFAULT,
      accentColor: WIDGET_ACCENT_COLOR_DEFAULT,
      backgroundColor: WIDGET_BACKGROUND_COLOR_DEFAULT,
      textColor: WIDGET_TEXT_COLOR_DEFAULT,
      brandColorExplicit: false,
      botName: WIDGET_BOT_NAME_DEFAULT,
      botNameExplicit: false,
      position: "right",
      width: WIDGET_DEFAULT_WIDTH,
      height: WIDGET_DEFAULT_HEIGHT,
      metadataOverrides: {},
      extraMetadata: {},
      disablePrefetch: false,
      apiUrl: "",
      logoUrl: "",
      greeting: "",
      sessionTtlHours: 24,
      idleTimeoutMinutes: 30,
    };
  }

  const params = new URLSearchParams(window.location.search);
  const embedMode = parseBooleanParam(params.get("embed"), false);
  const widgetKey = String(params.get("widget_key") || "").trim();
  const hotelCode = String(params.get("hotel_code") || "")
    .trim()
    .toLowerCase();
  const phase = normalizePhaseId(params.get("phase") || "pre_booking") || "pre_booking";
  const position = String(params.get("position") || "")
    .trim()
    .toLowerCase() === "left"
    ? "left"
    : "right";
  const width = parseNumberParam(params.get("width"), WIDGET_DEFAULT_WIDTH, 280, 600);
  const height = parseNumberParam(params.get("height"), WIDGET_DEFAULT_HEIGHT, 360, 900);
  const sessionId = String(params.get("session_id") || "").trim() || generateSessionId();
  const widgetId = String(params.get("widget_id") || params.get("widget_key") || "default").trim() || "default";
  const brandColor = normalizeColorParam(params.get("brand_color") || WIDGET_BRAND_COLOR_DEFAULT, WIDGET_BRAND_COLOR_DEFAULT);
  const accentColor = normalizeColorParam(
    params.get("accent_color") || params.get("brand_color") || WIDGET_ACCENT_COLOR_DEFAULT,
    WIDGET_ACCENT_COLOR_DEFAULT,
  );
  const backgroundColor = normalizeColorParam(
    params.get("bg_color") || WIDGET_BACKGROUND_COLOR_DEFAULT,
    WIDGET_BACKGROUND_COLOR_DEFAULT,
  );
  const textColor = normalizeColorParam(params.get("text_color") || WIDGET_TEXT_COLOR_DEFAULT, WIDGET_TEXT_COLOR_DEFAULT);
  // Treat a URL-pinned brand color / bot name as an *explicit override* only when
  // it's a genuine customization — i.e. different from the built-in default. The
  // embed loader always pins these params (defaulting to #FF6816 / "Kebo"), so a
  // plain truthiness check made every embed look "explicit" and suppressed the
  // DB-configured theme resolved via /api/widget/bootstrap below. Comparing
  // against the default lets the DB win whenever the host didn't actually
  // customize the value. (Case-insensitive: the loader strips the leading # and
  // hex case can vary.)
  const brandColorParam = String(params.get("brand_color") || "").trim();
  const brandColorExplicit =
    Boolean(brandColorParam) &&
    normalizeColorParam(brandColorParam, WIDGET_BRAND_COLOR_DEFAULT).toLowerCase() !==
      WIDGET_BRAND_COLOR_DEFAULT.toLowerCase();
  const botNameParam = String(params.get("bot_name") || "").trim();
  const botNameExplicit =
    Boolean(botNameParam) && botNameParam.toLowerCase() !== WIDGET_BOT_NAME_DEFAULT.toLowerCase();
  const botName = botNameParam || WIDGET_BOT_NAME_DEFAULT;
  const metadataOverrides =
    parseProfile({
      guest_id: params.get("guest_id"),
      entity_id: params.get("entity_id"),
      organisation_id: params.get("organisation_id") || params.get("organization_id"),
      room_number: params.get("room_number"),
      guest_phone: params.get("guest_phone"),
      guest_name: params.get("guest_name"),
      group_id: params.get("group_id"),
      ticket_source: params.get("ticket_source"),
      flow: params.get("flow"),
    }) || {};
  const disablePrefetch = parseBooleanParam(params.get("disable_prefetch"), false);
  // api_url: explicit Kebo server URL for cross-origin embeds on hotel websites
  const apiUrl = String(params.get("api_url") || "").trim();
  const logoUrl = String(params.get("logo_url") || "").trim();
  const greeting = String(params.get("greeting") || params.get("welcome_message") || "").trim();
  const sessionTtlHours = parseNumberParam(params.get("session_ttl_hours"), 24, 1, 720);
  // 0 disables the idle timer; otherwise expires after N minutes of user inactivity.
  const idleTimeoutMinutes = parseNumberParam(params.get("idle_timeout_minutes"), 30, 0, 720);
  const extraMetadataRaw = String(params.get("extra_metadata") || "").trim();
  let extraMetadata: Record<string, unknown> = {};
  if (extraMetadataRaw) {
    try {
      const parsed = JSON.parse(extraMetadataRaw);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        extraMetadata = parsed as Record<string, unknown>;
      }
    } catch {
      extraMetadata = {};
    }
  }

  return {
    embedMode,
    widgetId,
    widgetKey,
    hotelCode,
    phase,
    sessionId,
    brandColor,
    accentColor,
    backgroundColor,
    textColor,
    brandColorExplicit,
    botName,
    botNameExplicit,
    position,
    width,
    height,
    metadataOverrides,
    extraMetadata,
    disablePrefetch,
    apiUrl,
    logoUrl,
    greeting,
    sessionTtlHours,
    idleTimeoutMinutes,
  };
}

function resolveTicketStatus(metadata: Record<string, unknown>): TicketStatus {
  const ticketId = String(metadata.ticket_id || "").trim();
  const ticketState = String(metadata.ticket_status || "")
    .trim()
    .toLowerCase();
  const ticketError = String(metadata.ticket_create_error || "").trim();
  const skipReason = String(
    metadata.ticket_create_skip_reason || metadata.ticket_skip_reason || "",
  ).trim();

  if (metadata.ticket_created === true || ticketId) {
    const stateSuffix = ticketState ? ` (${ticketState})` : "";
    return {
      label: `Created: ${ticketId || "unknown-id"}${stateSuffix}`,
      badge: "created",
    };
  }
  if (ticketError) {
    return {
      label: `Not created: ${ticketError}`,
      badge: "failed",
    };
  }
  if (metadata.ticket_created === false || skipReason) {
    return {
      label: skipReason ? `Not created: ${skipReason}` : "Not created",
      badge: "not-created",
    };
  }
  if (metadata.ticketing_required === true && metadata.ticketing_create_allowed === false) {
    return { label: "Not created: gated", badge: "not-created" };
  }
  if (metadata.ticketing_required === true) {
    return { label: "Ticket required", badge: "pending" };
  }
  return { label: "No ticket action", badge: "idle" };
}

function resolveTicketDetails(metadata: Record<string, unknown>): Record<string, unknown> | null {
  const ticketId = String(metadata.ticket_id || "").trim();
  const created = metadata.ticket_created === true || Boolean(ticketId);
  if (!created) return null;

  const apiResponse = metadata.ticket_api_response;
  if (apiResponse && typeof apiResponse === "object") {
    const payload = apiResponse as Record<string, unknown>;
    const rawRecord = payload.ticket_record || payload.record || payload.ticket;
    if (rawRecord && typeof rawRecord === "object") {
      return rawRecord as Record<string, unknown>;
    }
  }

  if (metadata.ticket_record && typeof metadata.ticket_record === "object") {
    return metadata.ticket_record as Record<string, unknown>;
  }

  const fallback: Record<string, unknown> = {};
  const keys = [
    "ticket_id",
    "ticket_status",
    "ticket_category",
    "ticket_sub_category",
    "ticket_priority",
    "ticket_summary",
    "ticket_source",
    "room_number",
    "ticket_service_id",
    "ticket_service_name",
  ];
  for (const key of keys) {
    const value = metadata[key];
    if (value !== undefined && value !== null && String(value).trim() !== "") {
      fallback[key] = value;
    }
  }
  if (apiResponse && typeof apiResponse === "object") {
    fallback.ticket_api_response = apiResponse;
  }
  return Object.keys(fallback).length > 0 ? fallback : null;
}

function parseProfile(input: unknown): ProfileMap | null {
  if (!input || typeof input !== "object") return null;
  const row = input as Record<string, unknown>;
  const keys = [
    "guest_id",
    "entity_id",
    "organisation_id",
    "room_number",
    "guest_phone",
    "guest_name",
    "guest_email",
    "check_in_date",
    "check_out_date",
    "group_id",
    "ticket_source",
    "flow",
  ];
  const parsed: ProfileMap = {};
  for (const key of keys) {
    const value = String(row[key] || "").trim();
    if (value) {
      (parsed as Record<string, string>)[key] = value;
    }
  }
  if (!parsed.organisation_id && parsed.entity_id) {
    parsed.organisation_id = parsed.entity_id;
  }
  if (!parsed.entity_id && parsed.organisation_id) {
    parsed.entity_id = parsed.organisation_id;
  }
  return Object.keys(parsed).length > 0 ? parsed : null;
}

const initialBookingDraft = {
  guest_phone: "",
  guest_name: "",
  property_name: "",
  room_number: "",
  room_type: "",
  check_in_date: "",
  check_out_date: "",
  num_guests: "1",
  status: "reserved",
};

/** True on a device whose primary input is a finger, i.e. one with a soft
 *  keyboard that programmatic focus would pull back over the conversation. */
const isTouchDevice = (): boolean => {
  try {
    return (
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(pointer: coarse)").matches
    );
  } catch {
    return false;
  }
};

const ChatHarness = () => {
  const runtimeConfig = useMemo(() => readEmbedRuntimeConfig(), []);
  const embedMode = runtimeConfig.embedMode;
  // Colors start from URL params (the embed loader always pins them). For a
  // direct /chat?hotel_code=... page no color param is present, so we resolve the
  // hotel's configured theme from its widget deployment below — keeping the
  // standalone page on-brand without changing the global default for other hotels.
  const [resolvedTheme, setResolvedTheme] = useState(() => ({
    brandColor: runtimeConfig.brandColor,
    accentColor: runtimeConfig.accentColor,
    backgroundColor: runtimeConfig.backgroundColor,
    textColor: runtimeConfig.textColor,
  }));
  // Opening lead-form copy. This component is shared by every tenant, so the
  // wording must come from the hotel's own config rather than being inlined.
  // Defaults are the strings that used to be hardcoded, so any hotel that has
  // not set its own renders exactly as before.
  const [leadFormCopy, setLeadFormCopy] = useState({
    title: "Hey there! Let's get you set up 😊",
    subtitle:
      "Just a few quick details so we can tailor your stay — your name, number, email, and the destination you have in mind. Then we'll dive right in!",
    consent: "",
  });
  // Which fields the opening form renders, in order — edited in Admin > Forms
  // and delivered by /api/widget/bootstrap. The defaults below are the four
  // fields this form used to hardcode, so a hotel with nothing saved (or a
  // bootstrap that fails) renders exactly as it always did.
  const [leadFormFields, setLeadFormFields] = useState<LeadFormField[]>(
    DEFAULT_LEAD_FORM_FIELDS,
  );
  const [leadFormSubmitLabel, setLeadFormSubmitLabel] = useState("Continue");
  // Header avatar image. Same precedence as the colors above: the embed loader's
  // ?logo_url= wins when present, otherwise the hotel's configured logo is
  // resolved from /api/widget/bootstrap below — so a direct /chat?hotel_code=
  // page shows the same avatar the embed does instead of the bot-name initial.
  const [resolvedLogoUrl, setResolvedLogoUrl] = useState(runtimeConfig.logoUrl);
  const widgetBrandColor = resolvedTheme.brandColor;
  const widgetAccentColor = resolvedTheme.accentColor;
  const widgetBackgroundColor = resolvedTheme.backgroundColor;
  const widgetTextColor = resolvedTheme.textColor;
  const widgetUiColor = widgetBrandColor || widgetAccentColor || WIDGET_BRAND_COLOR_DEFAULT;
  const widgetLogoUrl = resolvedLogoUrl;
  const widgetDefaultWidth = runtimeConfig.width;
  const widgetDefaultHeight = runtimeConfig.height;
  const widgetPosition = runtimeConfig.position;
  const widgetDockClass = widgetPosition === "left" ? "left-4" : "right-4";

  // Dedicated, long-lived key for the stable visitor session id.
  const SESSION_ID_KEY = useMemo(
    () => sessionIdStorageKey(runtimeConfig.widgetId, runtimeConfig.hotelCode),
    [runtimeConfig.widgetId, runtimeConfig.hotelCode],
  );
  // get-or-create the session id: an explicit ?session_id= wins (and is pinned),
  // otherwise reuse the stored stable id, otherwise generate one and store it.
  const [sessionId, setSessionId] = useState(() => {
    const key = sessionIdStorageKey(runtimeConfig.widgetId, runtimeConfig.hotelCode);
    let urlSid = "";
    try {
      urlSid = (new URLSearchParams(window.location.search).get("session_id") || "").trim();
    } catch {
      urlSid = "";
    }
    if (urlSid) {
      writeStoredSessionId(key, urlSid);
      return urlSid;
    }
    const stored = readStoredSessionId(key);
    if (stored) return stored;
    const fresh = runtimeConfig.sessionId || generateSessionId();
    writeStoredSessionId(key, fresh);
    return fresh;
  });
  // Conversation ID = the current chat thread. Rotates on "new chat"; sent on
  // every WS message alongside the (stable) session id.
  // Embed: the loader passes a per-browser conversation id via the iframe URL
  // (it owns the resume/rotate lifecycle in the host page's first-party storage,
  // because the iframe's own storage is partitioned/blocked). Honor it exactly
  // as we honor ?session_id=. Direct /chat has no param → a fresh id.
  const [conversationId, setConversationId] = useState(() => {
    try {
      const urlCid = (new URLSearchParams(window.location.search).get("conversation_id") || "").trim();
      if (urlCid) return urlCid;
    } catch { /* ignore */ }
    return newConversationId();
  });
  // ── localStorage persistence ──────────────────────────────────────────────
  const STORAGE_KEY = useMemo(
    () => `kebo_session_${runtimeConfig.widgetId || runtimeConfig.hotelCode || "default"}`,
    [runtimeConfig.widgetId, runtimeConfig.hotelCode],
  );
  const SESSION_TTL_MS = (runtimeConfig.sessionTtlHours || 24) * 60 * 60 * 1000;

  // Restore from localStorage on first mount
  const [restoredFromStorage, setRestoredFromStorage] = useState(false);
  const [hotelCode, setHotelCode] = useState(runtimeConfig.hotelCode);

  // Resolve the selected hotel's configured theme (Bot Training > Step 3 colors,
  // served via /api/widget/bootstrap). Keyed off the active hotelCode so it works
  // for both the test harness (hotel dropdown) and a direct /chat?hotel_code= page.
  // Skipped when brand_color was pinned via URL (embed loader path) — that wins.
  useEffect(() => {
    const hotel = String(hotelCode || "").trim();
    if (!hotel) return;
    let cancelled = false;
    (async () => {
      try {
        const base = String(runtimeConfig.apiUrl || "").replace(/\/$/, "");
        const resp = await fetch(`${base}/api/widget/bootstrap?hotel_code=${encodeURIComponent(hotel)}`, {
          headers: { Accept: "application/json" },
        });
        if (!resp.ok) return;
        const boot = await resp.json();
        if (cancelled) return;
        // Lead-form copy is per-hotel and is fetched even when the embed loader
        // pinned the colors via URL — the two are unrelated.
        const ctas = (boot && boot.welcome_service_ctas) || null;
        if (Array.isArray(ctas)) {
          setWelcomeServiceCtas(ctas.map((c: unknown) => String(c || "").trim()).filter(Boolean));
        }
        const pb = (boot && boot.prebooking) || null;
        if (pb && typeof pb === "object") {
          const rawButtons = Array.isArray(pb.buttons) ? (pb.buttons as unknown[]) : [];
          setPrebookingConfig({
            buttons: rawButtons
              .filter((b): b is Record<string, unknown> => Boolean(b) && typeof b === "object")
              .map((b) => ({
                label: String(b.label || "").trim(),
                action: String(b.action || "message") === "intro" ? ("intro" as const) : ("message" as const),
                message: String(b.message || "").trim(),
              }))
              .filter((b) => b.label),
            after_lead_form: String(pb.after_lead_form || "intro") === "buttons" ? "buttons" : "intro",
          });
        } else {
          setPrebookingConfig(null);
        }
        const lf = (boot && boot.lead_form) || null;
        if (lf) {
          setLeadFormCopy((prev) => ({
            title: String(lf.title || "").trim() || prev.title,
            subtitle: String(lf.subtitle || "").trim() || prev.subtitle,
            consent: String(lf.consent || "").trim(),
          }));
          // Only adopt a field list that actually has fields: an empty array
          // would blank the form, so fall through to the built-in four.
          if (Array.isArray(lf.fields) && lf.fields.length > 0) {
            setLeadFormFields(
              lf.fields
                .filter((f: unknown) => f && typeof f === "object")
                .map((f: Record<string, unknown>) => ({
                  label: String(f.label || "").trim(),
                  type: String(f.type || "text"),
                  fieldType: (String(f.fieldType || "CUSTOM") as LeadFieldType),
                  required: Boolean(f.required),
                  placeholder: String(f.placeholder || ""),
                  options: Array.isArray(f.options)
                    ? (f.options as unknown[]).map((o) => String(o))
                    : undefined,
                }))
                .filter((f) => f.label),
            );
          }
          const submitLabel = String(lf.submit_label || "").trim();
          if (submitLabel) setLeadFormSubmitLabel(submitLabel);
        }
        // Logo is independent of the color params — the loader only pins it when
        // the deployment already had one, so an unpinned page must still resolve it.
        // Assigned even when empty so switching hotels in the harness drops the
        // previous hotel's avatar instead of carrying it over.
        const bootLogo = String((boot && boot.logo_url) || "").trim();
        if (!runtimeConfig.logoUrl) setResolvedLogoUrl(bootLogo);
        const t = (boot && boot.theme) || null;
        if (runtimeConfig.brandColorExplicit || !t) return;
        setResolvedTheme((prev) => ({
          brandColor: normalizeColorParam(t.brand_color || "", prev.brandColor),
          accentColor: normalizeColorParam(t.accent_color || t.brand_color || "", prev.accentColor),
          backgroundColor: normalizeColorParam(t.bg_color || "", prev.backgroundColor),
          textColor: normalizeColorParam(t.text_color || "", prev.textColor),
        }));
      } catch {
        /* keep defaults on any failure */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runtimeConfig.brandColorExplicit, runtimeConfig.apiUrl, runtimeConfig.logoUrl, hotelCode]);

  const [properties, setProperties] = useState<PropertyOption[]>([]);
  const [phases, setPhases] = useState<PhaseOption[]>(PHASE_FALLBACK);
  const [phase, setPhase] = useState(runtimeConfig.phase);
  const [testProfilesByPhase, setTestProfilesByPhase] = useState<Record<string, ProfileMap>>({});
  const [autoPhaseProfile, setAutoPhaseProfile] = useState(true);
  const [profileOverrides, setProfileOverrides] = useState<ProfileMap>(runtimeConfig.metadataOverrides);
  const [extraMetadataRaw, setExtraMetadataRaw] = useState(() =>
    Object.keys(runtimeConfig.extraMetadata).length > 0 ? JSON.stringify(runtimeConfig.extraMetadata, null, 2) : "",
  );

  // Runtime LLM model override (harness-only). "" = use env-configured models.
  const [modelOptions, setModelOptions] = useState<Array<{ id: string; label: string }>>([]);
  const [activeModel, setActiveModel] = useState("");
  const [defaultModel, setDefaultModel] = useState("");
  const [modelSaving, setModelSaving] = useState(false);
  // MULTI-MODEL: each role runs on its own model (routing vs answering/chips), so
  // the single-model selector is disabled — picking one would collapse the split.
  const [multiModelEnabled, setMultiModelEnabled] = useState(false);
  const [roleModels, setRoleModels] = useState<Record<string, string>>({});

  const [bookings, setBookings] = useState<BookingRow[]>([]);
  const [selectedBookingId, setSelectedBookingId] = useState<number | "">("");
  const [bookingModalOpen, setBookingModalOpen] = useState(false);
  const [bookingDraft, setBookingDraft] = useState(initialBookingDraft);
  const [bookingDraftError, setBookingDraftError] = useState("");
  const [bookingDraftSubmitting, setBookingDraftSubmitting] = useState(false);

  const [messages, setMessages] = useState<ChatMessageRow[]>([]);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  // Long assistant bubbles collapse to a "Read more" (WhatsApp-style). Tracks
  // which message ids the guest has expanded; everything else over the length
  // threshold renders clamped with a fade + toggle.
  const [expandedMsgs, setExpandedMsgs] = useState<Set<string>>(new Set());
  const toggleMsgExpanded = useCallback((id: string) => {
    setExpandedMsgs((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);
  const [input, setInput] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [isTyping, setIsTyping] = useState(false);
  // ── Agent handoff state ────────────────────────────────────────────────
  const [isAgent, setIsAgent] = useState(false);
  const [agentName, setAgentName] = useState("");
  // "bot" = normal bot mode | "waiting" = escalated, no agent yet | "agent_active" = agent joined
  const [agentHandoverStatus, setAgentHandoverStatus] = useState<"bot" | "waiting" | "agent_active">("bot");
  const [isAgentTyping, setIsAgentTyping] = useState(false);
  // Stable ref so onTyping can suppress bot-typing when agent is active without causing reconnects
  const isAgentRef = useRef(false);
  // Bridge: resolves when the next WebSocket response frame arrives
  const wsResponseResolveRef = useRef<((data: ChatApiResponse) => void) | null>(null);
  // Tracks the id of the user message currently in-flight (for status ticks)
  const pendingUserMsgIdRef = useRef<string>("");
  const [inlineForm, setInlineForm] = useState<InlineFormState | null>(null);
  // Booking-assistance form (opened from the "Get help from our team" button
  // that sits beside "Book on website"). Kept separate from `inlineForm`, which
  // is the server-driven service form — this one is a fixed 4-field ask and
  // posts to its own endpoint that EDITS the existing room-booking ticket.
  const [assistForm, setAssistForm] = useState<AssistFormState | null>(null);
  // Messages whose assistance form has been submitted — the button does not
  // come back, so the guest cannot file the same details twice.
  const [assistDoneIds, setAssistDoneIds] = useState<Set<string>>(new Set());
  // Set once the booking-assistance form has been submitted in THIS conversation.
  // Only consulted when the backend marks the CTA `once_per_conversation` (the
  // team-only booking flow), so every other tenant keeps the per-message
  // behaviour. Cleared whenever the conversation changes or is reset.
  const [assistSubmittedThisConvo, setAssistSubmittedThisConvo] = useState(false);
  // Events twin of `assistSubmittedThisConvo`: once the enquiry is in, offering
  // the button again reads as if we lost the first one.
  const [eventSubmittedThisConvo, setEventSubmittedThisConvo] = useState(false);
  const [welcomeMessage, setWelcomeMessage] = useState(
    runtimeConfig.greeting || "Hi! How can I help you today?",
  );
  const [welcomeContext, setWelcomeContext] = useState({
    bot_name: "Assistant",
    business_name: "your property",
    city: "",
    guest_name: "",
  });
  // Bot name shown in the widget header. An explicit ?bot_name= URL param wins
  // (deployment override); otherwise prefer the hotel's configured bot_name from
  // the business config (e.g. "Zoogle"); fall back to the widget default only if
  // neither is set. This stops the header from showing the generic default when
  // the property has configured its own bot name.
  const widgetBotName = useMemo(() => {
    if (runtimeConfig.botNameExplicit && runtimeConfig.botName) return runtimeConfig.botName;
    const configured = String(welcomeContext.bot_name || "").trim();
    if (configured && configured.toLowerCase() !== "assistant") return configured;
    return runtimeConfig.botName || WIDGET_BOT_NAME_DEFAULT;
  }, [runtimeConfig.botNameExplicit, runtimeConfig.botName, welcomeContext.bot_name]);
  const [propertyCount, setPropertyCount] = useState(0);
  // Flips true once /onboarding/business has resolved (success OR failure),
  // so the UI can tell "single-property" apart from "not loaded yet".
  const [businessWelcomeLoaded, setBusinessWelcomeLoaded] = useState(false);
  const [locationManifest, setLocationManifest] = useState<LocationManifestEntry[]>([]);
  const [showWelcomeCta, setShowWelcomeCta] = useState(true);
  // Per-hotel CTA buttons offered once the conversation opens (after the intro
  // lead form, and again on a fresh conversation in the same session). Comes
  // from /api/widget/bootstrap; empty for any hotel not configured, so no other
  // tenant's widget changes.
  const [welcomeServiceCtas, setWelcomeServiceCtas] = useState<string[]>([]);
  // Admin-managed hotels only (null for every other hotel, which keeps the
  // built-in "Book a Stay" flow exactly as before).
  const [prebookingConfig, setPrebookingConfig] = useState<PrebookingWelcomeConfig | null>(null);
  // The opening button that sent a new visitor to the lead form first; it runs
  // as soon as the form is in.
  const pendingWelcomeButtonRef = useRef<AdminWelcomeButton | null>(null);
  // Which opening CTA the guest chose. Both open the same contact form; the
  // difference is the message sent once they submit it, so a venue enquiry
  // starts the conversation in the events lane rather than in rooms.
  const [welcomeCtaIntent, setWelcomeCtaIntent] = useState<"stay" | "venue">("stay");
  const [locationPickerOpen, setLocationPickerOpen] = useState(false);
  const [locationSearch, setLocationSearch] = useState("");
  // CTA "Book a Stay" contact form: clicking the CTA collects name/phone/email
  // up front, then proceeds to location selection (multi) or booking (single).
  // Once submitted, `bookingContact` is merged into every request's metadata so
  // the bot's known_context has the guest's details for the booking.
  const [ctaContactOpen, setCtaContactOpen] = useState(false);
  // Bumped on every conversation reset (new session / dustbin). Used as an
  // auto-open-form effect dependency so the effect ALWAYS re-evaluates after a
  // reset — even in the embed, where the conversation_id is loader-owned and
  // does not change on first load, so it alone can't retrigger the open.
  const [convoResetNonce, setConvoResetNonce] = useState(0);
  // `extras` holds answers to operator-added fields that have no dedicated slot
  // on the guest record; the four named slots stay as they were.
  const [ctaContactValues, setCtaContactValues] = useState<{
    name: string;
    phone: string;
    email: string;
    location: string;
    extras?: Record<string, string>;
  }>({ name: "", phone: "", email: "", location: "", extras: {} });
  const [ctaContactPhoneCode, setCtaContactPhoneCode] = useState<PhoneCountryCode>(DEFAULT_PHONE_COUNTRY);
  const [ctaContactErrors, setCtaContactErrors] = useState<Record<string, string>>({});
  const [bookingContact, setBookingContact] = useState<{ guest_name: string; guest_phone: string; guest_email: string } | null>(null);
  // Ticket id of the booking lead created on CTA submit. Once set, later turns
  // carry it so the backend skips re-opening the booking form (no duplicate
  // ticket) and updates this same lead instead.
  const [bookingLeadId, setBookingLeadId] = useState("");
  const BOOKING_LEAD_SERVICE_ID = "room_booking_request";
  // Once the guest clicks the "Book on website" button, stop auto-showing it on
  // later room/booking turns. It only comes back when the guest explicitly asks
  // for the link / to book on the website (the agent re-emits it then).
  const [bookingLinkClicked, setBookingLinkClicked] = useState(false);
  // Multi-property location selection state. No auto-gate at start; once the
  // guest starts chatting without a location, the dropdown is shown and the
  // input is locked to selection. After they pick, the dropdown stays visible
  // but locked (so they can see all locations but not re-select).
  const [locationChosen, setLocationChosen] = useState(false);
  const [chosenLocationLabel, setChosenLocationLabel] = useState("");

  const [sessionState, setSessionState] = useState("idle");
  const [messageCount, setMessageCount] = useState(0);
  const [ticketStatus, setTicketStatus] = useState<TicketStatus>({
    label: "No ticket action",
    badge: "idle",
  });
  const [ticketDetails, setTicketDetails] = useState<Record<string, unknown> | null>(null);
  const [debugData, setDebugData] = useState<Record<string, unknown> | null>(null);
  const [widgetOpen, setWidgetOpen] = useState(embedMode);
  const [widgetMinimized, setWidgetMinimized] = useState(false);
  const [widgetExpanded, setWidgetExpanded] = useState(false);
  const [widgetShowTeaser, setWidgetShowTeaser] = useState(!embedMode);
  const isMobile = useIsMobile();
  const [mobileFormStep, setMobileFormStep] = useState(0);
  const [mobileFormStepTransitioning, setMobileFormStepTransitioning] = useState(false);

  // Reset to step 0 whenever a new form opens
  useEffect(() => {
    setMobileFormStep(0);
    setMobileFormStepTransitioning(false);
  }, [inlineForm?.messageId]);

  const activeHotelCode = useMemo(() => String(hotelCode || "").trim().toLowerCase(), [hotelCode]);

  // ── WebSocket transport ─────────────────────────────────────────────────
  const { status: wsStatus, serverHealthy, sendMessage: wsSend } = useWebSocketChat({
    sessionId,
    hotelCode: activeHotelCode,
    channel: "web_widget",
    conversationId,
    phase: normalizePhaseId(phase) || "pre_booking",
    apiUrl: runtimeConfig.apiUrl || undefined,
    // Embed only — makes the server apply this deployment's pinned model.
    widgetKey: runtimeConfig.widgetKey || undefined,
    onResponse: useCallback((data: ChatApiResponse) => {
      setIsTyping(false);
      // Resolve the pending promise so sendMessage continues
      if (wsResponseResolveRef.current) {
        wsResponseResolveRef.current(data);
        wsResponseResolveRef.current = null;
      }
    }, []),
    onTyping: useCallback(() => {
      // Suppress bot typing indicator when a human agent is active
      if (!isAgentRef.current) {
        setIsTyping(true);
      }
    }, []),
    onError: useCallback((detail: string) => {
      setIsTyping(false);
      // Reject the pending promise by resolving with an error marker
      if (wsResponseResolveRef.current) {
        wsResponseResolveRef.current({ message: `Error: ${detail}` } as ChatApiResponse);
        wsResponseResolveRef.current = null;
      }
    }, []),
    onHandover: useCallback((frame: Record<string, unknown>) => {
      const toResponder = String(frame.to_responder || "").toUpperCase();
      const handoverMsg = String(frame.message || "").trim();
      const botFollowup = String(frame.bot_followup || "").trim();
      const frameAgentName = String(frame.agent_name || "").trim();
      if (toResponder === "WAITING") {
        isAgentRef.current = false;
        setIsAgent(false);
        setAgentHandoverStatus("waiting");
      } else if (toResponder === "AGENT") {
        isAgentRef.current = true;
        setIsAgent(true);
        setAgentName(frameAgentName);
        setAgentHandoverStatus("agent_active");
        setIsTyping(false);
        setIsAgentTyping(false);
      } else if (toResponder === "BOT") {
        isAgentRef.current = false;
        setIsAgent(false);
        setAgentName("");
        setAgentHandoverStatus("bot");
        setIsAgentTyping(false);
      }
      // Add a centered system banner in the chat, then — when the bot resumes —
      // a real assistant bubble so the guest sees the bot actively check in.
      if (handoverMsg || botFollowup) {
        setMessages((prev) => {
          const next = [...prev];
          if (handoverMsg) {
            next.push({
              id: `sys_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
              role: "system" as Role,
              content: handoverMsg,
              canonicalContent: handoverMsg,
              timestamp: Date.now(),
            });
          }
          if (botFollowup && toResponder === "BOT") {
            next.push({
              id: `bot_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
              role: "assistant" as Role,
              content: botFollowup,
              canonicalContent: botFollowup,
              timestamp: Date.now() + 1,
            });
          }
          return next;
        });
      }
    }, []),
    onAgentMessage: useCallback((frame: Record<string, unknown>) => {
      const text = String(frame.text || "").trim();
      const frameAgentName = String(frame.agent_name || "Agent").trim();
      setIsTyping(false);
      setIsAgentTyping(false);
      if (text) {
        setAgentName(frameAgentName);
        setMessages((prev) => [
          ...prev,
          {
            id: `agent_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
            role: "agent" as Role,
            content: text,
            canonicalContent: text,
            timestamp: Date.now(),
          },
        ]);
      }
    }, []),
    onAgentTyping: useCallback(() => {
      setIsAgentTyping(true);
      // Auto-clear after 3s — agent WS doesn't send a "stopped typing" event
      setTimeout(() => setIsAgentTyping(false), 3000);
    }, []),
  });

  // The header dot means "the assistant can answer you", not "a socket is open".
  // Those came apart in production: with the database down the WebSocket still
  // connected and still ponged, so the widget sat there claiming Online while
  // every turn failed. The server now reports its own health on the connect and
  // pong frames, and BOTH have to hold before we say Online.
  const botOnline = wsStatus === "open" && serverHealthy;
  const botStatusLabel = botOnline
    ? "Online"
    : wsStatus === "connecting"
      ? "Connecting..."
      : wsStatus === "open"
        // Reachable but not serving — "Offline" would be a lie in the other
        // direction, and the guest needs to know the difference between "your
        // message went nowhere" and "we're having trouble answering".
        ? "Unavailable"
        : "Offline";

  const [historyOpen, setHistoryOpen] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyData, setHistoryData] = useState<SessionHistory | null>(null);
  const [historyError, setHistoryError] = useState("");

  const [ticketsOpen, setTicketsOpen] = useState(false);
  const [ticketsLoading, setTicketsLoading] = useState(false);
  const [tickets, setTickets] = useState<TicketRow[]>([]);
  const [ticketsError, setTicketsError] = useState("");

  const suggestionRequestIdRef = useRef(0);
  const prefetchBatchIdRef = useRef("");
  const messageEndRef = useRef<HTMLDivElement | null>(null);
  const messagesScrollRef = useRef<HTMLDivElement | null>(null);
  const messageRowRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const lastScrollAnchorRef = useRef("");
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  const postEmbedEvent = useCallback(
    (type: string, payload?: Record<string, unknown>) => {
      if (!embedMode || typeof window === "undefined" || window.parent === window) return;
      try {
        window.parent.postMessage(
          {
            source: "kebo-widget",
            type,
            widget_id: runtimeConfig.widgetId,
            hotel_code: hotelCode,
            phase: normalizePhaseId(phase) || "pre_booking",
            ...(payload || {}),
          },
          "*",
        );
      } catch {
        // no-op
      }
    },
    [embedMode, hotelCode, phase, runtimeConfig.widgetId],
  );

  const selectedBooking = useMemo(() => {
    if (selectedBookingId === "") return null;
    return bookings.find((row) => row.booking_id === selectedBookingId) || null;
  }, [bookings, selectedBookingId]);

  // BOOKING IDENTITY IS AUTHORITATIVE (post-booking sessions). When a booking
  // with a known guest is active, the guest is ALREADY known from their
  // confirmed booking — so the greeting + every ticket/form must use the
  // BOOKING's guest, and the self-entered lead-capture form is skipped
  // (introFormRequired needs !bookingContact). This stops a stale saved lead
  // (e.g. an earlier "Sana Shaaista") from overriding the booking's real guest
  // (e.g. "sna") in the welcome and in tickets. No booking (e.g. Zone
  // pre-booking) → this does nothing and the lead-capture flow is untouched.
  useEffect(() => {
    const gname = String(selectedBooking?.guest_name || "").trim();
    if (!gname) return;
    const gphone = String(selectedBooking?.guest_phone || "").trim();
    const gemail = String((selectedBooking as { guest_email?: string } | null)?.guest_email || "").trim();
    setBookingContact((prev) => {
      if (prev && prev.guest_name === gname && prev.guest_phone === gphone) return prev;
      return { guest_name: gname, guest_phone: gphone, guest_email: gemail };
    });
    setWelcomeContext((prev) => (prev.guest_name === gname ? prev : { ...prev, guest_name: gname }));
  }, [selectedBooking]);

  const resolvedWelcomeMessage = useMemo(
    () =>
      formatWelcomeMessage(
        welcomeMessage,
        welcomeContext,
        // Several properties that all sit in ONE location (two hotels in the
        // same city) have no location to choose, so they get the hotel's own
        // greeting rather than the "choose the location" line.
        (propertyCount > 1 || locationManifest.length > 1) && locationManifest.length !== 1,
        phase,
      ),
    [welcomeContext, welcomeMessage, propertyCount, locationManifest.length, phase],
  );
  // Read by the restore effect, which must not re-run (and re-fetch the whole
  // history) every time the greeting text is recomputed.
  const resolvedWelcomeMessageRef = useRef(resolvedWelcomeMessage);
  resolvedWelcomeMessageRef.current = resolvedWelcomeMessage;

  // CTA quick-action(s) shown on the welcome bubble. Single-property pre-checkin
  // guests get a "Book transport shuttle" chip that routes straight to the
  // transport/arrival service when clicked.
  const welcomeSuggestedActions = useMemo(() => {
    const isMulti = propertyCount > 1 || locationManifest.length > 1;
    if (isMulti) return [];
    const normalizedPhase = String(phase || "").toLowerCase().replace(/[-\s]+/g, "_");
    if (normalizedPhase === "pre_checkin") return ["Book transport shuttle"];
    // In-house guests: the three most common in-stay actions.
    if (normalizedPhase === "during_stay") return ["Order food", "Housekeeping", "Reserve a table", "Book a spa"];
    return [];
  }, [propertyCount, locationManifest.length, phase]);

  const activePhaseProfile = useMemo(() => {
    if (!autoPhaseProfile) return null;
    const normalized = normalizePhaseId(phase);
    if (!normalized) return null;
    const mapped = testProfilesByPhase[normalized];
    if (!mapped) return null;
    const resolved: ProfileMap = { ...mapped };
    for (const [key, value] of Object.entries(profileOverrides)) {
      if (!String(value || "").trim()) continue;
      (resolved as Record<string, string>)[key] = String(value).trim();
    }
    if (!resolved.organisation_id && resolved.entity_id) {
      resolved.organisation_id = resolved.entity_id;
    }
    if (!resolved.entity_id && resolved.organisation_id) {
      resolved.entity_id = resolved.organisation_id;
    }
    return resolved;
  }, [autoPhaseProfile, phase, profileOverrides, testProfilesByPhase]);

  // Keep a ref so sendMessage (which has a stable dep array) can always read the
  // latest activePhaseProfile without stale-closure issues.
  const activePhaseProfileRef = useRef<ProfileMap | null>(null);
  activePhaseProfileRef.current = activePhaseProfile;

  // GUEST-LOOKUP IDENTITY (harness): when NO local test booking is selected but a
  // real guest profile is active (resolved per-phase from GHN_FF_GUEST_INFO — e.g.
  // Sana1 for pre_checkin, Sana2 for during_stay), treat THAT guest as the known
  // guest: greet them by name and skip the lead-capture form. Fully DYNAMIC — it
  // reflects whichever guest's profile is active for the phase, never a hardcoded
  // name. A selected local booking still wins (handled by the effect above).
  useEffect(() => {
    if (selectedBooking && String(selectedBooking.guest_name || "").trim()) return;
    const gname = String(activePhaseProfile?.guest_name || "").trim();
    if (!gname) return;
    const gphone = String(activePhaseProfile?.guest_phone || "").trim();
    const gemail = String(activePhaseProfile?.guest_email || "").trim();
    setBookingContact((prev) => {
      if (prev && prev.guest_name === gname && prev.guest_phone === gphone) return prev;
      return { guest_name: gname, guest_phone: gphone, guest_email: gemail };
    });
    setWelcomeContext((prev) => (prev.guest_name === gname ? prev : { ...prev, guest_name: gname }));
  }, [activePhaseProfile, selectedBooking]);

  const updateSessionUiFromResponse = useCallback((payload: ChatApiResponse) => {
    const nextState = String(payload.state || "idle").trim() || "idle";
    setSessionState(nextState);
    const metadata = (payload.metadata || {}) as Record<string, unknown>;
    const nextMessageCount = Number(metadata.message_count || 0);
    if (Number.isFinite(nextMessageCount) && nextMessageCount > 0) {
      setMessageCount(nextMessageCount);
    } else {
      setMessageCount((prev) => prev + 1);
    }
    setTicketStatus(resolveTicketStatus(metadata));
    setTicketDetails(resolveTicketDetails(metadata));
  }, []);

  // Clears the on-screen conversation to a fresh welcome state and mints a new
  // conversation id. Shared by "new chat" (same session) and full session reset.
  // prefillLead (name/phone/email) re-shows the intro contact form PREFILLED so
  // a known visitor just confirms/edits their identity and picks a fresh
  // location; pass null/omit for a brand-new (blank) form.
  const resetConversationUi = useCallback((opts?: {
    prefillLead?: { guest_name?: string; guest_phone?: string; guest_email?: string } | null;
    returning?: boolean;
    conversationId?: string;
  }) => {
    // Booking identity wins: if a booking with a known guest is active, use IT
    // as the (returning) identity — the guest is already known, so greet by the
    // booking name and skip the lead-capture form, regardless of any stale saved
    // lead. No booking → fall back to the passed-in lead (pre-booking flow).
    const bookingLead = (() => {
      const gname = String(selectedBooking?.guest_name || "").trim();
      if (gname) {
        return {
          guest_name: gname,
          guest_phone: String(selectedBooking?.guest_phone || "").trim(),
          guest_email: String((selectedBooking as { guest_email?: string } | null)?.guest_email || "").trim(),
        };
      }
      // No local test booking selected — fall back to the active guest-lookup
      // profile (per-phase real guest: Sana1 pre_checkin, Sana2 during_stay) so
      // the welcome greets them by name. Dynamic per whichever guest is active.
      const pname = String(activePhaseProfile?.guest_name || "").trim();
      if (pname) {
        return {
          guest_name: pname,
          guest_phone: String(activePhaseProfile?.guest_phone || "").trim(),
          guest_email: String(activePhaseProfile?.guest_email || "").trim(),
        };
      }
      return null;
    })();
    const lead = bookingLead || opts?.prefillLead || null;
    // A returning visitor already has a captured (frozen) lead: we DON'T re-show
    // the intro contact form — we greet them by name and only ask for location.
    const isReturning = Boolean(bookingLead) || Boolean(opts?.returning && lead && lead.guest_name);
    // Clear saved conversation so next mount starts fresh
    try { localStorage.removeItem(STORAGE_KEY); } catch { /* noop */ }
    // A fresh conversation must not inherit the previously selected booking.
    // If it does, every new turn keeps sending the old booking's property name
    // and room context, which re-pins the backend to that hotel.
    setSelectedBookingId("");
    // Embed passes a loader-owned conversation id to keep the thread stable
    // across the iframe's blocked storage; otherwise mint a fresh thread id.
    setConversationId(opts?.conversationId || newConversationId());
    setMessages([
      {
        id: `welcome_${Date.now()}`,
        role: "assistant",
        content: resolvedWelcomeMessage,
        canonicalContent: resolvedWelcomeMessage,
        suggested_actions: welcomeSuggestedActions,
        timestamp: Date.now(),
      },
    ]);
    setSuggestions([]);
    setInlineForm(null);
    setSessionState("idle");
    setMessageCount(0);
    setTicketStatus({ label: "No ticket action", badge: "idle" });
    setTicketDetails(null);
    setDebugData(null);
    prefetchBatchIdRef.current = "";
    suggestionRequestIdRef.current += 1;
    setLocationPickerOpen(false);
    setLocationSearch("");
    setCtaContactErrors({});
    setBookingLinkClicked(false);
    setBookingLeadId("");
    // Fresh thread: clear chosen location (re-picked per thread) and clear the
    // lead flag so the intro form re-shows — but PREFILL name/phone/email if we
    // know them, so the guest just confirms (or edits) and picks a location.
    setLocationChosen(false);
    setChosenLocationLabel("");
    // Returning visitor: keep the frozen lead as bookingContact so the intro
    // form is SKIPPED (introFormRequired needs !bookingContact), and personalise
    // the welcome by name. New visitor: clear it so the intro form shows once.
    if (isReturning && lead) {
      setBookingContact({
        guest_name: String(lead.guest_name || ""),
        guest_phone: String(lead.guest_phone || ""),
        guest_email: String(lead.guest_email || ""),
      });
      setWelcomeContext((prev) => ({ ...prev, guest_name: String(lead.guest_name || "") }));
    } else {
      setBookingContact(null);
      setWelcomeContext((prev) => ({ ...prev, guest_name: "" }));
    }
    setShowWelcomeCta(true);
    setCtaContactValues({
      name: String(lead?.guest_name || ""),
      phone: String(lead?.guest_phone || "").replace(/\D/g, "").slice(-10),
      email: String(lead?.guest_email || ""),
      location: "",
    });
    setCtaContactOpen(false); // for a NEW visitor, the introFormRequired effect re-opens it (prefilled)
    setAssistDoneIds(new Set());
    setAssistSubmittedThisConvo(false); // a new conversation may book again
    setEventSubmittedThisConvo(false); // ...and may raise a fresh event enquiry
    setConvoResetNonce((n) => n + 1); // force the auto-open effect to re-evaluate
  }, [resolvedWelcomeMessage, STORAGE_KEY, selectedBooking, activePhaseProfile]);

  // Full session reset (idle timeout / "New Session" debug button): a brand-new
  // VISITOR session id, pinned as the stable id, plus a fresh conversation.
  const resetLocalConversation = useCallback((newSessionId: string) => {
    writeStoredSessionId(SESSION_ID_KEY, newSessionId);
    setSessionId(newSessionId);
    resetConversationUi();
  }, [SESSION_ID_KEY, resetConversationUi]);

  // Fetch the saved lead profile for this stable visitor id (name/phone/email)
  // so a new conversation's intro form can be prefilled even after a reload,
  // when the in-memory bookingContact has been lost. Best effort → null.
  const fetchSessionProfile = useCallback(async (
    sid: string,
  ): Promise<{ guest_name: string; guest_phone: string; guest_email: string } | null> => {
    if (!sid) return null;
    try {
      const base = String(runtimeConfig.apiUrl || "").replace(/\/$/, "");
      const res = await fetch(`${base}/api/chat/session/${encodeURIComponent(sid)}/profile`);
      if (!res.ok) return null;
      const data = await res.json();
      const name = String(data?.guest_name || "").trim();
      const phone = String(data?.guest_phone || "").trim();
      const email = String(data?.guest_email || "").trim();
      if (!name && !phone && !email) return null;
      return { guest_name: name, guest_phone: phone, guest_email: email };
    } catch {
      return null;
    }
  }, [runtimeConfig.apiUrl]);

  // Start a brand-new conversation thread for the SAME visitor. Shows the intro
  // form PREFILLED with the known identity (name/phone/email) — location left
  // blank so it is chosen fresh per thread. Uses the in-memory lead if present,
  // else fetches the saved profile (reload / 30-min-idle return case).
  const beginNewConversation = useCallback(async (opts?: { resetBackend?: boolean; conversationId?: string }) => {
    const sid = sessionId;
    let lead = bookingContact;
    if (!lead || !(lead.guest_name || lead.guest_phone || lead.guest_email)) {
      lead = await fetchSessionProfile(sid);
    }
    // A known visitor (frozen lead exists) returns to a personalised welcome +
    // location dropdown — NOT the intro form. A brand-new visitor gets the form.
    const isReturning = Boolean(lead && lead.guest_name);
    resetConversationUi({ prefillLead: lead, returning: isReturning, conversationId: opts?.conversationId });
    if (opts?.resetBackend && sid) {
      const base = String(runtimeConfig.apiUrl || "").replace(/\/$/, "");
      void fetch(`${base}/api/chat/session/${encodeURIComponent(sid)}/reset`, {
        method: "POST",
      }).catch(() => { /* best effort */ });
    }
  }, [sessionId, bookingContact, fetchSessionProfile, resetConversationUi, runtimeConfig.apiUrl]);

  // "New chat" (dustbin icon): SAME visitor session, brand-new conversation
  // thread, and clear the bot's server-side context so the thread truly starts
  // fresh (kebo's clear-chat behaviour). Best effort — never blocks the UI.
  const startNewChat = useCallback(() => {
    void beginNewConversation({ resetBackend: true });
  }, [beginNewConversation]);

  const fetchAndShowSuggestions = useCallback(
    async (
      lastBotMessage: string,
      userMessage: string,
      assistantTurnId: string,
      attempt: number = 0,
    ) => {
      if (inlineForm && !inlineForm.successMessage) {
        setSuggestions([]);
        return;
      }
      const requestId = ++suggestionRequestIdRef.current;
      // Do not clear suggestions here; caller already handles the display
      // transition around each assistant turn to avoid chip flicker.
      try {
        const response = await fetch("/api/chat/suggestions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            last_bot_message: lastBotMessage,
            user_message: userMessage,
            hotel_code: hotelCode,
            current_phase: phase,
            session_id: sessionId,
            conversation_id: conversationId,
            assistant_turn_id: assistantTurnId,
          }),
        });
        if (!response.ok) {
          // Strictly no fallback chips. If the backend errors, show nothing.
          return;
        }
        const data = (await response.json()) as SuggestionsApiResponse;
        if (requestId !== suggestionRequestIdRef.current) return;
        if (inlineForm && !inlineForm.successMessage) return;
        prefetchBatchIdRef.current = String(data.prefetch_batch_id || "").trim();
        const next = Array.isArray(data.suggestions) ? data.suggestions.filter(Boolean) : [];
        if (next.length > 0) {
          setSuggestions(next);
          return;
        }
        // Progressive retry. Chip generation can run 5–25s depending on KB
        // size and model latency. We keep polling on a long, gentle cadence
        // so late chips still appear instead of being silently dropped.
        // Total wall-clock budget ~30s.
        const retryDelays = [600, 1000, 1500, 2500, 3500, 5000, 7000, 9000];
        const serverRetryAfter = Number((data as { retry_after_ms?: number }).retry_after_ms || 0);
        const isPending = String((data as { status?: string }).status || "") === "pending";
        if (attempt < retryDelays.length && (isPending || attempt < 3)) {
          const baseDelay = retryDelays[attempt];
          const delay = serverRetryAfter > 0
            ? Math.max(baseDelay, Math.min(serverRetryAfter, 9000))
            : baseDelay;
          window.setTimeout(() => {
            if (requestId === suggestionRequestIdRef.current) {
              void fetchAndShowSuggestions(
                lastBotMessage,
                userMessage,
                assistantTurnId,
                attempt + 1,
              );
            }
          }, delay);
          return;
        }
        // Strictly no fallback chips. If nothing is ready after retries, show nothing.
        setSuggestions([]);
      } catch {
        // Strictly no fallback chips on network errors either.
      }
    },
    [hotelCode, inlineForm, phase, sessionId],
  );

  const loadProperties = useCallback(async () => {
    if (embedMode) {
      const embeddedCode = String(runtimeConfig.hotelCode || "").trim().toLowerCase();
      if (embeddedCode) {
        setProperties([{ code: embeddedCode, name: embeddedCode, city: "" }]);
        setHotelCode(embeddedCode);
      } else {
        setProperties([]);
        setHotelCode("");
      }
      return;
    }
    try {
      const response = await fetch("/api/chat/properties");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = (await response.json()) as { properties?: Array<Record<string, unknown>> };
      const rows = Array.isArray(payload.properties) ? payload.properties : [];
      const parsed = rows
        .map((row) => {
          const code = String(row.code || "").trim().toLowerCase();
          if (!code) return null;
          return {
            code,
            name: String(row.name || code).trim() || code,
            city: String(row.city || "").trim(),
          } satisfies PropertyOption;
        })
        .filter((row): row is PropertyOption => Boolean(row));
      if (parsed.length === 0) throw new Error("No properties");
      setProperties(parsed);
      setHotelCode((prev) => {
        const current = String(prev || "").trim().toLowerCase();
        if (parsed.some((row) => row.code === current)) return current;
        return parsed[0].code;
      });
    } catch {
      // Never force-switch to a synthetic "default" property on transient errors.
      setProperties((prev) => prev);
      setHotelCode((prev) => {
        const current = String(prev || "").trim().toLowerCase();
        if (current && current !== "default") return current;
        const runtimeCode = String(runtimeConfig.hotelCode || "").trim().toLowerCase();
        if (runtimeCode && runtimeCode !== "default") return runtimeCode;
        return "";
      });
    }
  }, [embedMode, runtimeConfig.hotelCode]);

  const loadTestProfiles = useCallback(async () => {
    // Customer embeds must never inherit the harness's guest/test profiles.
    // Those profiles inject entity_id / ticket_source / flow metadata that is
    // useful for QA, but harmful in real pre-booking widget conversations:
    // lead-capture turns can get reclassified as old-stay guest sessions and
    // the backend then overwrites the current property scope with unrelated
    // historical stay data.
    if (embedMode) {
      setTestProfilesByPhase({});
      setAutoPhaseProfile(false);
      setPhases(PHASE_FALLBACK);
      setPhase((prev) => normalizePhaseId(prev) || "pre_booking");
      return;
    }
    if (!activeHotelCode) {
      setTestProfilesByPhase({});
      setAutoPhaseProfile(false);
      setPhases(PHASE_FALLBACK);
      setPhase((prev) => normalizePhaseId(prev) || "pre_booking");
      return;
    }
    try {
      const response = await fetch(`/api/chat/test-profiles?hotel_code=${encodeURIComponent(activeHotelCode)}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = (await response.json()) as {
        auto_apply_enabled?: boolean;
        profiles_by_phase?: Record<string, unknown>;
        phases?: Array<Record<string, unknown>>;
      };
      const mapped: Record<string, ProfileMap> = {};
      if (payload.profiles_by_phase && typeof payload.profiles_by_phase === "object") {
        for (const [key, value] of Object.entries(payload.profiles_by_phase)) {
          const normalized = normalizePhaseId(key);
          if (!normalized) continue;
          const parsed = parseProfile(value);
          if (parsed) {
            mapped[normalized] = parsed;
          }
        }
      }
      setTestProfilesByPhase(mapped);
      setAutoPhaseProfile(payload.auto_apply_enabled !== false);

      const phaseRows = Array.isArray(payload.phases) ? payload.phases : [];
      const parsedPhases = phaseRows
        .map((row) => {
          const id = normalizePhaseId(row.id);
          if (!id) return null;
          return {
            id,
            name: String(row.name || id.replace(/_/g, " ")).trim(),
          } satisfies PhaseOption;
        })
        .filter((row): row is PhaseOption => Boolean(row));
      const nextPhases = parsedPhases.length > 0 ? parsedPhases : PHASE_FALLBACK;
      setPhases(nextPhases);
      setPhase((prev) => {
        const normalized = normalizePhaseId(prev);
        if (normalized && nextPhases.some((row) => row.id === normalized)) return normalized;
        return nextPhases[0]?.id || "pre_booking";
      });
    } catch {
      setTestProfilesByPhase({});
      setAutoPhaseProfile(false);
      setPhases(PHASE_FALLBACK);
      setPhase((prev) => normalizePhaseId(prev) || "pre_booking");
    }
  }, [activeHotelCode, embedMode]);

  const loadBusinessWelcome = useCallback(async () => {
    if (!activeHotelCode) {
      setWelcomeContext((prev) => ({
        ...prev,
        bot_name: "Assistant",
        business_name: "your property",
        city: "",
      }));
      setWelcomeMessage("Hi! How can I help you today?");
      setBusinessWelcomeLoaded(true);
      return;
    }
    try {
      const response = await fetch("/admin/api/config/onboarding/business", {
        headers: { "x-hotel-code": activeHotelCode },
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = (await response.json()) as Record<string, unknown>;
      setWelcomeContext((prev) => ({
        ...prev,
        bot_name: String(payload.bot_name || "Assistant").trim() || "Assistant",
        business_name: String(payload.name || payload.business_name || "your property").trim() || "your property",
        city: String(payload.city || "").trim(),
      }));
      setWelcomeMessage(
        String(
          payload.resolved_welcome_message ||
            payload.multi_property_welcome_message ||
            payload.welcome_message ||
            "",
        ).trim() || "Hi! How can I help you today?",
      );
      const rawCount = Number(payload.property_count || 0);
      setPropertyCount(Number.isFinite(rawCount) && rawCount > 0 ? Math.floor(rawCount) : 0);
      const manifestRaw = Array.isArray(payload.multi_property_location_manifest)
        ? (payload.multi_property_location_manifest as Array<Record<string, unknown>>)
        : [];
      const manifest: LocationManifestEntry[] = manifestRaw
        .map((entry) => {
          const label = String((entry || {}).label || "").trim();
          if (!label) return null;
          const names = Array.isArray((entry || {}).property_names)
            ? ((entry as Record<string, unknown>).property_names as unknown[])
                .map((name) => String(name || "").trim())
                .filter((name) => Boolean(name))
            : [];
          return { label, property_names: names };
        })
        .filter((row): row is LocationManifestEntry => Boolean(row));
      setLocationManifest(manifest);
      setBusinessWelcomeLoaded(true);
    } catch {
      setWelcomeContext((prev) => ({
        ...prev,
        bot_name: "Assistant",
        business_name: "your property",
        city: "",
      }));
      setWelcomeMessage("Hi! How can I help you today?");
      setPropertyCount(0);
      setLocationManifest([]);
      setBusinessWelcomeLoaded(true);
    }
  }, [activeHotelCode]);

  const loadBookings = useCallback(async () => {
    if (embedMode) {
      setBookings([]);
      setSelectedBookingId("");
      return;
    }
    const normalizedPhase = normalizePhaseId(phase);
    if (!activeHotelCode || !normalizedPhase || normalizedPhase === "pre_booking") {
      setBookings([]);
      setSelectedBookingId("");
      return;
    }
    try {
      const response = await fetch(
        `/admin/api/bookings?hotel_code=${encodeURIComponent(activeHotelCode)}&phase=${encodeURIComponent(normalizedPhase)}`,
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = (await response.json()) as { bookings?: BookingRow[] };
      const rows = Array.isArray(payload.bookings) ? payload.bookings : [];
      setBookings(rows);
      setSelectedBookingId((prev) => {
        if (typeof prev === "number" && rows.some((row) => row.booking_id === prev)) return prev;
        return "";
      });
    } catch {
      setBookings([]);
      setSelectedBookingId("");
    }
  }, [activeHotelCode, embedMode, phase]);

  useEffect(() => {
    void loadProperties();
  }, [loadProperties]);

  // ── Runtime model override (harness-only) ───────────────────────────────
  // Lets QA pick which OpenAI chat model every LLM call uses in the deployed
  // build, without editing .env + redeploying. Applied globally server-side.
  const applyModelOverrideState = useCallback(
    (payload: {
      available?: unknown;
      active?: unknown;
      default_model?: unknown;
      multi_model_enabled?: unknown;
      role_models?: unknown;
    }) => {
      const rows = Array.isArray(payload.available) ? payload.available : [];
      const parsed = rows
        .map((row) => {
          const r = (row || {}) as Record<string, unknown>;
          const id = String(r.id || "").trim();
          if (!id) return null;
          return { id, label: String(r.label || id).trim() || id };
        })
        .filter((r): r is { id: string; label: string } => Boolean(r));
      setModelOptions(parsed);
      setActiveModel(String(payload.active || "").trim());
      setDefaultModel(String(payload.default_model || "").trim());
      setMultiModelEnabled(Boolean(payload.multi_model_enabled));
      const roles = (payload.role_models || {}) as Record<string, unknown>;
      setRoleModels(
        Object.fromEntries(
          Object.entries(roles).map(([role, id]) => [role, String(id || "").trim()]),
        ),
      );
    },
    [],
  );

  const loadModelOverride = useCallback(async () => {
    if (embedMode) return;
    try {
      const response = await fetch("/api/chat/model-override");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      applyModelOverrideState(await response.json());
    } catch {
      // Non-fatal: the selector just stays empty if the endpoint is unavailable.
    }
  }, [embedMode, applyModelOverrideState]);

  const handleModelOverrideChange = useCallback(
    async (nextModel: string) => {
      const previous = activeModel;
      setActiveModel(nextModel); // optimistic
      setModelSaving(true);
      try {
        const response = await fetch("/api/chat/model-override", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ model: nextModel }),
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        applyModelOverrideState(await response.json());
      } catch {
        setActiveModel(previous); // revert on failure
      } finally {
        setModelSaving(false);
      }
    },
    [activeModel, applyModelOverrideState],
  );

  useEffect(() => {
    void loadModelOverride();
  }, [loadModelOverride]);

  // Restore conversation on first mount — SERVER-DRIVEN, session-scoped (kebo
  // model). The backend is the source of truth for this stable visitor's chat
  // history; we fetch the session's messages by session_id instead of rebuilding
  // from localStorage + idle/TTL math. localStorage is kept only as a fast cache
  // (the save effect below) and as an offline fallback if the fetch fails.
  useEffect(() => {
    if (restoredFromStorage) return;
    let cancelled = false;
    const sid = sessionId;
    // Embed: the loader owns the conversation id (it survives the iframe's
    // partitioned storage and already applied resume/rotate first-party). When
    // present it is authoritative — adopt it instead of re-deciding here.
    let urlConvId = "";
    try {
      urlConvId = (new URLSearchParams(window.location.search).get("conversation_id") || "").trim();
    } catch { /* ignore */ }

    const mapServerMessages = (raw: unknown): ChatMessageRow[] => {
      const list = Array.isArray(raw) ? raw : [];
      const rows: ChatMessageRow[] = [];
      list.forEach((m, i) => {
        const item = (m && typeof m === "object") ? (m as Record<string, unknown>) : {};
        const content = String(item.content || "").trim();
        if (!content) return;
        const role: Role = String(item.role || "assistant") === "user" ? "user" : "assistant";
        const tsRaw = item.timestamp ? Date.parse(String(item.timestamp)) : NaN;

        // Replay what the guest actually SAW, not just the plain text. The
        // server stores the turn's rich display — beautified text, the form
        // that was filled in, a menu download card, the photos an answer came
        // with — and a live turn renders all of that from `raw`. Rebuilding
        // `raw` here is what makes a reloaded conversation look like the one
        // the guest was just having, instead of a stripped-down transcript.
        const meta = (item.metadata && typeof item.metadata === "object")
          ? (item.metadata as Record<string, unknown>)
          : {};
        const dp = (meta.display_payload && typeof meta.display_payload === "object")
          ? (meta.display_payload as Record<string, unknown>)
          : null;

        // Internal markers are stripped from the OUTGOING reply, but the message
        // was already persisted BEFORE that happened - so history still carries
        // them, and a replayed turn showed "<<SHOW_BOOKING_BUTTON: ...>>" as text.
        const stripMarkers = (v: string) =>
          v
            .replace(/<<[^<>]*>>/g, "")
            .replace(/[^\S\r\n]+$/gm, "")
            .replace(/\n{3,}/g, "\n\n")
            .trim();
        const cleanContent = stripMarkers(content);
        const display = dp ? stripMarkers(String(dp.display_message || "")) : "";
        const raw = dp
          ? ({
              message: cleanContent,
              ...(display ? { display_message: display } : {}),
              ...(Array.isArray(dp.form_fields) && dp.form_fields.length
                ? { form_fields: dp.form_fields as Array<Record<string, unknown>> }
                : {}),
              ...(dp.form_service_id ? { form_service_id: String(dp.form_service_id) } : {}),
              metadata: {
                ...(Array.isArray(dp.images) && dp.images.length ? { images: dp.images } : {}),
                ...(Array.isArray(dp.documents) && dp.documents.length
                  ? { documents: dp.documents }
                  : {}),
                // The "Book on website" button renders from this; without it a
                // replayed booking turn lost its button.
                ...(dp.booking_cta ? { booking_cta: dp.booking_cta } : {}),
                // ...and the "Get help from our team" button that sits under it.
                // It rendered from metadata too, so a reload used to bring the
                // website link back on its own with the help button missing.
                ...(dp.assist_cta ? { assist_cta: dp.assist_cta } : {}),
                ...(dp.form_cancellable ? { form_cancellable: true } : {}),
                // Admin-set link buttons of the service that answered.
                ...(Array.isArray(dp.service_links) && dp.service_links.length
                  ? { service_links: dp.service_links }
                  : {}),
              },
            } as ChatApiResponse)
          : undefined;

        rows.push({
          id: `srv_${i}`,
          role,
          // The beautified text is what was on screen; the plain text stays as
          // the canonical copy the rest of the app reasons about.
          content: display || cleanContent,
          canonicalContent: cleanContent,
          ...(raw ? { raw } : {}),
          timestamp: Number.isFinite(tsRaw) ? tsRaw : Date.now(),
        });
      });
      return rows;
    };

    // The opening greeting is generated on the CLIENT and never persisted, so a
    // restored conversation began abruptly on the guest's first question - the
    // "Hi <name>, welcome back!" line they had actually seen was missing. Put it
    // back at the top so a reloaded chat reads exactly like the one they left.
    // Only the text: the location picker that came with it would be wrong now
    // that they have already chosen one.
    const withOpeningGreeting = (rows: ChatMessageRow[]): ChatMessageRow[] => {
      if (rows.length === 0) return rows;
      if (rows[0]?.role === "assistant" && rows[0]?.id?.startsWith("welcome_")) return rows;
      const greeting = String(resolvedWelcomeMessageRef.current || "").trim();
      if (!greeting) return rows;
      return [
        {
          id: `welcome_restored_${rows[0]?.timestamp || Date.now()}`,
          role: "assistant" as Role,
          content: greeting,
          canonicalContent: greeting,
          timestamp: (rows[0]?.timestamp || Date.now()) - 1,
        },
        ...rows,
      ];
    };

    // A form the guest was still filling in when they reloaded. The form is
    // React state set by the LIVE turn, so a reload replayed the message that
    // introduced it and showed no form at all — the guest was left staring at
    // "please fill in the details below" with nothing to fill in. Re-open it
    // when the LAST restored row is the assistant turn that carried it:
    // submitting posts a user row after it, so an already-submitted form can
    // never match here. Typed-but-unsent values are not on the server, so it
    // comes back exactly as it was first served.
    const reopenPendingForm = (rows: ChatMessageRow[]) => {
      const last = rows[rows.length - 1];
      if (!last || last.role !== "assistant") return;
      const fields = normalizeFormFields(last.raw?.form_fields);
      if (fields.length === 0) return;
      // No service id means a submit would have nowhere to go — leave the form
      // closed rather than showing one whose Submit button does nothing.
      const serviceId = String(last.raw?.form_service_id || "").trim();
      if (!serviceId) return;
      const values: Record<string, string> = {};
      const countryCodes: Record<string, PhoneCountryCode> = {};
      for (const field of fields) {
        const resolved = field.defaultValue || "";
        if (isPhoneField(field)) {
          const split = splitPhone(resolved);
          values[field.id] = split.digits;
          countryCodes[field.id] = split.country;
        } else {
          values[field.id] = resolved;
        }
      }
      setInlineForm({
        messageId: last.id,
        serviceId,
        fields,
        values,
        countryCodes,
        errors: {},
        submitting: false,
        successMessage: "",
        cancellable: Boolean(
          ((last.raw?.metadata || {}) as Record<string, unknown>).form_cancellable,
        ),
        cancelling: false,
      });
      setSuggestions([]);
    };

    // ── conversation_id lifecycle (kebo model) ───────────────────────────────
    // The WIDGET shows ONLY the current conversation_id's messages — never the
    // whole session. We RESUME the stored conversation on every load; it rotates
    // to a NEW thread ONLY when the guest clicks the dustbin — never on 30-min
    // idle or a new tab. session_id is stable throughout (lead + tickets).
    const CONVO_KEY = `nb_convo_state_${sid}`;
    const LIVE_KEY = `nb_convo_live_${sid}`;
    let storedConvId = "";
    try {
      const s = JSON.parse(localStorage.getItem(CONVO_KEY) || "{}");
      storedConvId = String(s.conversationId || "").trim();
    } catch { /* ignore */ }
    const resume = !!storedConvId;

    (async () => {
      // ── Embed: adopt the loader-owned conversation id (authoritative). ──
      // Restore this conversation's server history (same-tab reload); if there
      // is none yet, start it fresh but KEEP the loader's id so the thread is
      // stable across the iframe's blocked storage.
      if (urlConvId) {
        try {
          const base = String(runtimeConfig.apiUrl || "").replace(/\/$/, "");
          const res = await fetch(`${base}/api/chat/session/${encodeURIComponent(sid)}`);
          if (!cancelled && res.ok) {
            const data = await res.json();
            const all = Array.isArray(data?.messages) ? data.messages : [];
            const scoped = all.filter((m) => {
              const meta = (m && typeof m === "object" ? (m as Record<string, unknown>).metadata : null) || {};
              return String((meta as Record<string, unknown>).conversation_uid || "").trim() === urlConvId;
            });
            const rows = mapServerMessages(scoped);
            if (rows.length > 0) {
              setConversationId(urlConvId);
              setAssistSubmittedThisConvo(
                data?.booking_assistance?.conversation_id === urlConvId &&
                data.booking_assistance.details_submitted === true,
              );
              try { sessionStorage.setItem(LIVE_KEY, urlConvId); } catch { /* ignore */ }
              if (data?.state) setSessionState(String(data.state));
              setMessages(withOpeningGreeting(rows));
              setMessageCount(rows.length);
              reopenPendingForm(rows);
              restoreChipsForRows(rows, urlConvId);
              setRestoredFromStorage(true);
              return;
            }
          }
        } catch { /* fall through: start this conversation fresh, keeping the id */ }
        if (!cancelled) {
          await beginNewConversation({ conversationId: urlConvId });
          setRestoredFromStorage(true);
        }
        return;
      }
      if (resume) {
        try {
          const base = String(runtimeConfig.apiUrl || "").replace(/\/$/, "");
          const res = await fetch(`${base}/api/chat/session/${encodeURIComponent(sid)}`);
          if (!cancelled && res.ok) {
            const data = await res.json();
            const all = Array.isArray(data?.messages) ? data.messages : [];
            // CONVERSATION-SCOPED: only this conversation's messages.
            const scoped = all.filter((m) => {
              const meta = (m && typeof m === "object" ? (m as Record<string, unknown>).metadata : null) || {};
              return String((meta as Record<string, unknown>).conversation_uid || "").trim() === storedConvId;
            });
            const rows = mapServerMessages(scoped);
            if (rows.length > 0) {
              setConversationId(storedConvId);
              setAssistSubmittedThisConvo(
                data?.booking_assistance?.conversation_id === storedConvId &&
                data.booking_assistance.details_submitted === true,
              );
              try { sessionStorage.setItem(LIVE_KEY, storedConvId); } catch { /* ignore */ }
              if (data?.state) setSessionState(String(data.state));
              setMessages(withOpeningGreeting(rows));
              setMessageCount(rows.length);
              reopenPendingForm(rows);
              restoreChipsForRows(rows, storedConvId);
              setRestoredFromStorage(true);
              return;
            }
          }
        } catch { /* fall through to a fresh conversation */ }
      }
      if (cancelled) return;
      // No resumable messages: KEEP the stored id if we have one (only the
      // dustbin rotates); a brand-new visitor with no stored id gets a fresh one.
      // Returning visitor → 'Hi {Name}!' + location; brand-new → intro form.
      await beginNewConversation(storedConvId ? { conversationId: storedConvId } : undefined);
      if (!cancelled) setRestoredFromStorage(true);
    })();

    return () => { cancelled = true; };
  }, [sessionId, restoredFromStorage, runtimeConfig.apiUrl, beginNewConversation]);

  // Put the stored chips back, but ONLY when the restored conversation ends on
  // the very reply they were generated for. Anything else (a newer answer, a
  // different thread) and they are dropped rather than shown against the wrong
  // message.
  const restoreChipsForRows = useCallback(
    (rows: ChatMessage[], convId: string) => {
      try {
        const raw = localStorage.getItem(`${STORAGE_KEY}_chips`);
        if (!raw) return;
        const saved = JSON.parse(raw) as {
          conversationId?: string; forBotMessage?: string; chips?: string[];
        };
        const chips = Array.isArray(saved.chips) ? saved.chips.filter(Boolean) : [];
        if (chips.length === 0) return;
        if (String(saved.conversationId || "") !== String(convId || "")) return;
        const lastBot = [...rows].reverse().find((m) => m.role === "assistant");
        if (!lastBot) return;
        const restoredTail = String(lastBot.content || "").trim().slice(0, 400);
        if (restoredTail !== String(saved.forBotMessage || "")) return;
        setSuggestions(chips);
      } catch {
        /* unreadable storage - the guest just sees no chips, as before */
      }
    },
    [STORAGE_KEY],
  );

  // ── CHIPS SURVIVE A RELOAD ────────────────────────────────────────────
  // Chips are fetched per turn and held in memory only, so a browser refresh
  // wiped them: the guest came back to the same last reply, still on screen,
  // with its buttons intact and nothing left to tap. They are stored here
  // against the reply they belong to, and put back when that same reply is
  // the one restored - never carried onto a different or newer answer.
  const CHIPS_KEY = `${STORAGE_KEY}_chips`;
  useEffect(() => {
    if (!restoredFromStorage || !sessionId) return;
    try {
      if (suggestions.length === 0) {
        localStorage.removeItem(CHIPS_KEY);
        return;
      }
      const lastBot = [...messages].reverse().find((m) => m.role === "assistant");
      if (!lastBot) return;
      localStorage.setItem(CHIPS_KEY, JSON.stringify({
        conversationId,
        forBotMessage: String(lastBot.content || "").trim().slice(0, 400),
        chips: suggestions,
        savedAt: Date.now(),
      }));
    } catch {
      // storage full or unavailable - chips simply won't survive the reload
    }
  }, [CHIPS_KEY, restoredFromStorage, sessionId, conversationId, suggestions, messages]);

  // Save conversation to localStorage whenever messages change
  useEffect(() => {
    if (!restoredFromStorage) return;
    if (!sessionId || messages.length === 0) return;
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({
        sessionId,
        conversationId,
        messages,
        sessionState,
        messageCount,
        savedAt: Date.now(),
        hotelCode: activeHotelCode,
      }));
    } catch {
      // storage full or unavailable — ignore
    }
  }, [STORAGE_KEY, restoredFromStorage, sessionId, conversationId, messages, sessionState, messageCount, activeHotelCode]);

  // Persist the conversation_id POINTER (+ last-activity ts) and mark it "live"
  // in sessionStorage. The restore effect reads these to RESUME on a same-tab
  // reload, and to start a NEW conversation on a fresh visit/new tab or after a
  // 30-min idle gap. Kept separate from the message cache above so the pointer
  // is tiny and always current.
  useEffect(() => {
    if (!restoredFromStorage || !sessionId || !conversationId) return;
    try {
      localStorage.setItem(
        `nb_convo_state_${sessionId}`,
        JSON.stringify({ conversationId, lastActivityTs: Date.now() }),
      );
    } catch { /* ignore */ }
    try { sessionStorage.setItem(`nb_convo_live_${sessionId}`, conversationId); } catch { /* ignore */ }
    // Embed: the iframe's storage is partitioned, so mirror the conversation
    // pointer to the host loader (first-party). This keeps a dustbin "new chat"
    // rotation authoritative on the next reload instead of resurrecting the old
    // thread the loader originally handed us.
    postEmbedEvent("conversation", { conversation_id: conversationId });
  }, [restoredFromStorage, sessionId, conversationId, messages, postEmbedEvent]);

  // ── Idle auto-reset: DISABLED (session-centric model) ─────────────────────
  // A stable visitor now keeps ONE continuous, server-stored chat history, so
  // returning to the tab simply resumes it — we no longer mint a fresh
  // conversation (prefilled intro form) after an idle gap. Kept as a no-op that
  // references the idle inputs so the deployment's idle_timeout_minutes param
  // remains harmless rather than removed.
  const idleTimeoutMinutes = runtimeConfig.idleTimeoutMinutes;
  useEffect(() => {
    void idleTimeoutMinutes;
  }, [idleTimeoutMinutes]);

  useEffect(() => {
    if (!hotelCode) return;
    void loadTestProfiles();
    void loadBusinessWelcome();
    void loadBookings();
  }, [hotelCode, loadBookings, loadBusinessWelcome, loadTestProfiles]);

  useEffect(() => {
    void loadBookings();
  }, [loadBookings, phase]);

  useEffect(() => {
    if (!embedMode) return;
    postEmbedEvent("widget:ready");
  }, [embedMode, hotelCode, phase, postEmbedEvent]);

  // Also runs for the opening lead form. On a short phone its Continue button
  // landed BELOW the fold while the composer sits disabled ("please fill in your
  // details above to begin") — so the guest saw a form with no visible button
  // and a box they could not type in, and read the whole widget as broken. It
  // was only ever a scroll away; now we do the scroll.
  useEffect(() => {
    if (!locationPickerOpen && !ctaContactOpen) return;
    if (typeof window === "undefined") return;
    const rafId = window.requestAnimationFrame(() => {
      const panel = messagesScrollRef.current;
      if (panel) {
        panel.scrollTop = panel.scrollHeight;
        return;
      }
      messageEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
    });
    return () => window.cancelAnimationFrame(rafId);
  }, [locationPickerOpen, ctaContactOpen]);

  const getWidgetFrameSize = useCallback(() => {
    const width = Math.min(widgetExpanded ? WIDGET_EXPANDED_WIDTH : widgetDefaultWidth, 600);
    if (widgetMinimized) {
      return { width, height: 56 };
    }
    const height = Math.min(widgetExpanded ? WIDGET_EXPANDED_HEIGHT : widgetDefaultHeight, 900);
    return { width, height };
  }, [widgetDefaultHeight, widgetDefaultWidth, widgetExpanded, widgetMinimized]);

  useEffect(() => {
    if (!embedMode) return;
    postEmbedEvent("widget:resize", getWidgetFrameSize());
  }, [embedMode, getWidgetFrameSize, postEmbedEvent]);

  useEffect(() => {
    const panel = messagesScrollRef.current;
    if (!panel || messages.length === 0 || typeof window === "undefined") return;
    const lastMessage = messages[messages.length - 1];
    if (lastMessage.role !== "assistant" && lastMessage.role !== "user") return;
    const latestUserMessage = [...messages].reverse().find((row) => row.role === "user");
    if (!latestUserMessage) return;

    const anchorKey = `${latestUserMessage.id}:${lastMessage.id}:${lastMessage.role}`;
    if (lastScrollAnchorRef.current === anchorKey) return;
    lastScrollAnchorRef.current = anchorKey;

    const rafId = window.requestAnimationFrame(() => {
      const currentPanel = messagesScrollRef.current;
      const anchorEl = messageRowRefs.current[latestUserMessage.id];
      if (!currentPanel || !anchorEl) return;
      currentPanel.scrollTop = Math.max(0, anchorEl.offsetTop - 12);
    });
    return () => window.cancelAnimationFrame(rafId);
  }, [messages]);

  // Mobile keyboard: keep the field being typed into visible.
  //
  // The embed locks html/body to height 100% + overflow hidden, so when the
  // on-screen keyboard opens there is no page scroll for the browser to use,
  // and inside an iframe iOS does not reposition the content either. Filling a
  // form on a phone therefore left the guest staring at the composer while they
  // typed into a field hidden behind the keyboard. The messages panel is the
  // only real scroller, so scroll it ourselves — but only when the field is
  // actually out of view, which makes this a no-op on desktop.
  //
  // `focusin` is bound to the document rather than the panel because the panel
  // mounts and unmounts with the widget; the panel is looked up per event.
  useEffect(() => {
    if (typeof window === "undefined" || typeof document === "undefined") return;

    let timer: number | undefined;

    const revealFocusedField = () => {
      const el = document.activeElement as HTMLElement | null;
      if (!el || !/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) return;
      const panel = messagesScrollRef.current;
      if (!panel || !panel.contains(el)) return;

      const field = el.getBoundingClientRect();
      const panelBox = panel.getBoundingClientRect();
      // The keyboard shrinks the VISUAL viewport, never the layout one, so this
      // is the only reliable read of where the covered area begins.
      const viewport = window.visualViewport;
      const viewportBottom = viewport ? viewport.offsetTop + viewport.height : window.innerHeight;
      const visibleBottom = Math.min(panelBox.bottom, viewportBottom);

      const coveredBelow = field.bottom > visibleBottom - 8;
      const cutOffAbove = field.top < panelBox.top + 8;
      if (!coveredBelow && !cutOffAbove) return;

      // Leave a third of the visible strip above the field so its label — and
      // any validation error under it — stay on screen too.
      const headroom = Math.max(48, (visibleBottom - panelBox.top) / 3);
      const target = panel.scrollTop + (field.top - panelBox.top) - headroom;
      panel.scrollTo({ top: Math.max(0, target), behavior: "smooth" });
    };

    // Wait for the keyboard to finish animating in before measuring anything.
    const schedule = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(revealFocusedField, 300);
    };

    document.addEventListener("focusin", schedule);
    const viewport = window.visualViewport;
    viewport?.addEventListener("resize", schedule);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("focusin", schedule);
      viewport?.removeEventListener("resize", schedule);
    };
  }, []);

  useEffect(() => {
    if (!embedMode || typeof document === "undefined") return;
    const html = document.documentElement;
    const body = document.body;
    const root = document.getElementById("chat-root");

    const prevHtmlHeight = html.style.height;
    const prevHtmlOverflow = html.style.overflow;
    const prevBodyHeight = body.style.height;
    const prevBodyOverflow = body.style.overflow;
    const prevRootHeight = root?.style.height || "";
    const prevRootOverflow = root?.style.overflow || "";

    html.style.height = "100%";
    html.style.overflow = "hidden";
    body.style.height = "100%";
    body.style.overflow = "hidden";
    if (root) {
      root.style.height = "100%";
      root.style.overflow = "hidden";
    }

    return () => {
      html.style.height = prevHtmlHeight;
      html.style.overflow = prevHtmlOverflow;
      body.style.height = prevBodyHeight;
      body.style.overflow = prevBodyOverflow;
      if (root) {
        root.style.height = prevRootHeight;
        root.style.overflow = prevRootOverflow;
      }
    };
  }, [embedMode]);

  useEffect(() => {
    // Wait until localStorage restore has completed before injecting the welcome
    // message — otherwise this effect fires at the same time as the restore effect
    // (both see messages.length === 0 on mount) and overwrites the restored history.
    if (!restoredFromStorage) return;
    if (messages.length === 0) {
      setMessages([
        {
          id: `welcome_${Date.now()}`,
          role: "assistant",
          content: resolvedWelcomeMessage,
          canonicalContent: resolvedWelcomeMessage,
          suggested_actions: welcomeSuggestedActions,
          timestamp: Date.now(),
        },
      ]);
      return;
    }
    if (
      messages.length === 1 &&
      messages[0]?.id?.startsWith("welcome_") &&
      String(messages[0].content || "") !== resolvedWelcomeMessage
    ) {
      setMessages([
        {
          id: messages[0].id,
          role: "assistant",
          content: resolvedWelcomeMessage,
          canonicalContent: resolvedWelcomeMessage,
          suggested_actions: welcomeSuggestedActions,
          timestamp: messages[0].timestamp ?? Date.now(),
        },
      ]);
      return;
    }
    // Hotels set up in the Pre-booking tab (T24 onward): a lead form sent before
    // the hotel's greeting arrived left the stand-in "Hi! How can I help you
    // today?" on top of the chat for good, because the greeting is refreshed only
    // while it is the only message. It is refreshed while it still shows that
    // stand-in. Zone, Mango and Sarovar unchanged.
    const standInGreeting = "Hi! How can I help you today?";
    if (
      prebookingConfig !== null &&
      messages.length > 1 &&
      messages[0]?.id?.startsWith("welcome_") &&
      String(messages[0].content || "") === standInGreeting &&
      resolvedWelcomeMessage &&
      resolvedWelcomeMessage !== standInGreeting
    ) {
      setMessages((prev) =>
        prev.length > 0 && prev[0]?.id?.startsWith("welcome_")
          ? [{ ...prev[0], content: resolvedWelcomeMessage, canonicalContent: resolvedWelcomeMessage }, ...prev.slice(1)]
          : prev,
      );
    }
  }, [messages, resolvedWelcomeMessage, restoredFromStorage, prebookingConfig]);


  const buildRequestMetadata = useCallback(
    (interactionMeta?: InteractionMeta) => {
      const phaseId = normalizePhaseId(phase) || "pre_booking";
      const metadata: Record<string, unknown> = {
        phase: phaseId,
        // Carry the current thread id so form-submit / HTTP turns land under the
        // same conversation_id as the WS chat turns (engage history threading).
        conversation_id: conversationId,
        chat_test_profile_applied: false,
        chat_test_profile_phase: phaseId,
      };

      if (interactionMeta) {
        const sourceType = String(interactionMeta.source_type || "").trim();
        const sourceLabel = String(interactionMeta.source_label || "").trim();
        const sourceText = String(interactionMeta.source_text || "").trim();
        if (sourceType) metadata.ui_source_type = sourceType;
        if (sourceLabel) metadata.ui_source_label = sourceLabel;
        if (sourceText) metadata.ui_source_text = sourceText;
        metadata.ui_event_at = new Date().toISOString();
      }

      if (activePhaseProfile) {
        for (const [key, value] of Object.entries(activePhaseProfile)) {
          if (!String(value || "").trim()) continue;
          metadata[key] = value;
        }
        metadata.chat_test_profile_applied = true;
      }

      // CTA contact form (Book a Stay): once collected, carry the guest's
      // details on every turn so the bot's known_context can prefill the
      // booking without re-asking.
      if (bookingContact) {
        if (bookingContact.guest_name) metadata.guest_name = bookingContact.guest_name;
        if (bookingContact.guest_phone) metadata.guest_phone = bookingContact.guest_phone;
        if (bookingContact.guest_email) metadata.guest_email = bookingContact.guest_email;
      }
      // Booking lead already created (CTA): only mark it active when the current
      // turn is explicitly resuming/proceeding with booking. Informational room
      // questions should not keep triggering the backend lead shortcut forever.
      if (shouldSendBookingLeadResumeMetadata(bookingLeadId, interactionMeta)) {
        metadata.booking_lead_active = true;
        metadata.booking_lead_id = bookingLeadId;
        metadata.booking_lead_service_id = BOOKING_LEAD_SERVICE_ID;
      }
      if (bookingLinkClicked) {
        // Suppress the auto "Book on website" button on later room turns once
        // they've already clicked it (unless they explicitly ask again).
        metadata.booking_link_clicked = true;
      }

      if (selectedBooking) {
        metadata.booking_id = selectedBooking.booking_id;
        metadata.booking_guest_id = selectedBooking.guest_id || "";
        metadata.booking_confirmation_code = selectedBooking.confirmation_code || "";
        metadata.booking_property_name = selectedBooking.property_name || "";
        metadata.booking_room_number = selectedBooking.room_number || "";
        metadata.booking_room_type = selectedBooking.room_type || "";
        metadata.booking_check_in_date = selectedBooking.check_in_date || "";
        metadata.booking_check_out_date = selectedBooking.check_out_date || "";
        metadata.booking_guest_name = selectedBooking.guest_name || "";
        metadata.booking_guest_phone = selectedBooking.guest_phone || "";
        metadata.booking_status = selectedBooking.status || "";
        metadata.booking_phase = selectedBooking.phase || "";
      } else if (activePhaseProfile && (activePhaseProfile.guest_id || activePhaseProfile.check_in_date)) {
        // No local test booking selected, but a real guest profile is active
        // (guest lookup). Surface that guest's booking context so the bot greets
        // them by name and answers their REAL check-in/out dates. Dynamic per
        // whichever guest's profile is active — never hardcoded.
        const p = activePhaseProfile;
        if (p.guest_id) {
          metadata.booking_id = p.guest_id;
          metadata.booking_guest_id = p.guest_id;
        }
        if (p.guest_name) metadata.booking_guest_name = p.guest_name;
        if (p.guest_phone) metadata.booking_guest_phone = p.guest_phone;
        if (p.room_number) metadata.booking_room_number = p.room_number;
        if (p.check_in_date) metadata.booking_check_in_date = p.check_in_date;
        if (p.check_out_date) metadata.booking_check_out_date = p.check_out_date;
        metadata.booking_phase = phaseId;
        if (p.guest_email && !metadata.guest_email) metadata.guest_email = p.guest_email;
      }

      if (extraMetadataRaw.trim()) {
        try {
          const parsed = JSON.parse(extraMetadataRaw);
          if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
            Object.assign(metadata, parsed as Record<string, unknown>);
          }
        } catch {
          metadata.extra_metadata_parse_error = "Invalid JSON";
        }
      }

      if (!metadata.organisation_id && metadata.entity_id) {
        metadata.organisation_id = metadata.entity_id;
      }
      if (!metadata.entity_id && metadata.organisation_id) {
        metadata.entity_id = metadata.organisation_id;
      }

      return metadata;
    },
    [activePhaseProfile, bookingContact, bookingLeadId, bookingLinkClicked, conversationId, extraMetadataRaw, phase, selectedBooking],
  );

  const updateMsgStatus = useCallback((id: string, status: MessageStatus) => {
    setMessages((prev) =>
      prev.map((m) => (m.id === id && m.role === "user" ? { ...m, status } : m)),
    );
  }, []);

  const sendMessage = useCallback(
    async (rawMessage: string, sourceType = "typed_input", extraMeta?: Record<string, unknown>) => {
      const text = String(rawMessage || "").trim();
      if (!text || isSending) return;

      // Embed: tell the host loader the conversation is active so its
      // first-party idle clock rotates on last MESSAGE, not last page load.
      postEmbedEvent("activity");

      const userId = `u_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
      setWidgetOpen(true);
      setInput("");
      setSuggestions([]);
      setShowWelcomeCta(false);
      setLocationPickerOpen(false);
      setLocationSearch("");
      if (sourceType !== "suggested_action") {
        prefetchBatchIdRef.current = "";
      }
      // Invalidate any in-flight /suggestions request from the previous turn
      // so its late reply can't race chips back onto the screen after the
      // user has already typed their next message.
      suggestionRequestIdRef.current += 1;
      pendingUserMsgIdRef.current = userId;
      setMessages((prev) => [
        ...prev,
        { id: userId, role: "user", content: text, canonicalContent: text, status: "sending", timestamp: Date.now() },
      ]);

      // ── Multi-property location gate ──────────────────────────────────────
      // If the guest typed a message but hasn't chosen a location yet (and this
      // is a multi-property hotel in pre-booking), respond with a location-picker
      // BOT MESSAGE (a dropdown bubble) instead of calling the bot. This avoids a
      // redundant "which city?" reply and premature chips before we know where.
      const needsLocationFirst =
        sourceType === "typed_input" &&
        propertyCount > 1 &&
        // One location holding every property: nothing to choose, so no gate.
        locationManifest.length > 1 &&
        !locationChosen &&
        (normalizePhaseId(phase) || "pre_booking") === "pre_booking" &&
        agentHandoverStatus !== "agent_active" &&
        !inlineForm &&
        !ctaContactOpen;
      if (needsLocationFirst) {
        updateMsgStatus(userId, "sent");
        setMessages((prev) => [
          ...prev,
          {
            id: `locpick_${Date.now()}`,
            role: "assistant",
            content: "I'd be happy to help! First, which city are you planning your stay in?",
            canonicalContent: "I'd be happy to help! First, which city are you planning your stay in?",
            kind: "location_picker",
            timestamp: Date.now(),
          },
        ]);
        return;
      }

      setIsSending(true);

      try {
        const requestMetadata = buildRequestMetadata({
          source_type: sourceType,
          source_label: sourceType === "typed_input" ? "typed_input" : text,
          source_text: text,
        });
        // e.g. `intro_city` from an admin-configured opening button, so the
        // backend serves that location's saved message.
        if (extraMeta) Object.assign(requestMetadata, extraMeta);
        // Tell the bot the guest has ALREADY handed over their booking details in
        // this conversation, so a second "I want to book this room" is answered
        // with the status of that request instead of pushing the same form again
        // (the button is already hidden, so re-offering it points at nothing).
        if (assistSubmittedThisConvo) {
          requestMetadata.booking_details_submitted = true;
        }
        if (sourceType === "suggested_action") {
          const activePrefetchBatchId = String(prefetchBatchIdRef.current || "").trim();
          if (activePrefetchBatchId) {
            requestMetadata.prefetch_batch_id = activePrefetchBatchId;
            requestMetadata.ui_prefetch_batch_id = activePrefetchBatchId;
          }
        }
        let data: ChatApiResponse;
        if (wsStatus === "open") {
          // ── WebSocket path ──────────────────────────────────────────────
          const wsPromise = new Promise<ChatApiResponse>((resolve) => {
            wsResponseResolveRef.current = resolve;
          });
          // Safety net: never let this await hang forever. The hook stays patient
          // while the socket is healthy (a slow LOCAL turn can take 15-30s+, and
          // it keeps waiting across a few windows before recovering over HTTP).
          // This must sit ABOVE the hook's total patience + one HTTP recovery so
          // the hook's own delivery/error wins first and we don't surface a
          // premature "timed out" while a good answer is still on its way.
          let safetyTimer: ReturnType<typeof setTimeout> | undefined;
          const safetyPromise = new Promise<ChatApiResponse>((resolve) => {
            safetyTimer = setTimeout(() => {
              wsResponseResolveRef.current = null;
              resolve({ message: "Error: timed out waiting for a response" } as ChatApiResponse);
            }, 180_000);
          });
          wsSend(text, requestMetadata as Record<string, unknown>);
          // Message left the client → ✓ sent
          updateMsgStatus(userId, "sent");
          data = await Promise.race([wsPromise, safetyPromise]);
          if (safetyTimer) clearTimeout(safetyTimer);
          if (!data || (typeof data.message === "string" && data.message.startsWith("Error:"))) {
            throw new Error(data?.message ?? "WebSocket error");
          }
          // Response arrived → ✓✓ delivered
          updateMsgStatus(userId, "delivered");
        } else {
          // ── HTTP fallback (original behaviour, unchanged) ───────────────
          const response = await fetch("/api/chat/message", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              session_id: sessionId,
              message: text,
              hotel_code: hotelCode,
              channel: "web_widget",
              metadata: requestMetadata,
            }),
          });
          if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
          }
          data = (await response.json()) as ChatApiResponse;
          // HTTP: single tick → delivered on response
          updateMsgStatus(userId, "delivered");
        }
        // When a human agent is handling this session, the backend returns a
        // silent ack (message: "", agent_handling: true). Don't show a bot bubble.
        const agentHandling = (data?.metadata as Record<string, unknown>)?.agent_handling === true;
        if (agentHandling) return;

        const assistantMessage = resolveAssistantDisplayMessage(data) || "No response";
        const assistantId = `a_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`;
        setMessages((prev) => [
          ...prev,
          {
            id: assistantId,
            role: "assistant",
            content: assistantMessage,
            canonicalContent: String(data.message || assistantMessage),
            raw: data,
            timestamp: Date.now(),
          },
        ]);

        setDebugData(data as Record<string, unknown>);
        updateSessionUiFromResponse(data);

        const metadata = (data.metadata || {}) as Record<string, unknown>;
        const assistantTurnId = String(metadata.turn_trace_id || "").trim();
        const fields = normalizeFormFields(
          Array.isArray(metadata.form_fields) ? metadata.form_fields : data.form_fields,
        );
        const rawFormTrigger = metadata.form_trigger;
        const rawFormCancellable = metadata.form_cancellable;
        const formCancellable =
          rawFormCancellable === true ||
          rawFormCancellable === "true" ||
          rawFormCancellable === 1 ||
          rawFormCancellable === "1";
        const explicitFormTrigger =
          rawFormTrigger === true || rawFormTrigger === "true" || rawFormTrigger === 1 || rawFormTrigger === "1";
        const stateAwaitingInfo = String(data.state || "")
          .trim()
          .toLowerCase() === "awaiting_info";
        const serviceLabel = String(data.service_llm_label || metadata.service_llm_label || "")
          .trim()
          .toLowerCase();
        const displayText = String(data.display_message || metadata.display_message || data.message || "")
          .trim()
          .toLowerCase();
        const messageHintsCollection =
          displayText.includes("please fill in the details below") ||
          displayText.includes("please fill in the booking details below") ||
          displayText.includes("please fill in the form below");
        const inferredFormTrigger =
          stateAwaitingInfo && Boolean(serviceLabel) && serviceLabel !== "main" && fields.length > 0 && messageHintsCollection;
        const shouldShowInlineForm = (explicitFormTrigger || inferredFormTrigger) && fields.length > 0;
        const orchestrationDecision = (metadata.orchestration_decision || metadata.decision || {}) as Record<string, unknown>;
        const resolvedServiceId = String(
          metadata.form_service_id ||
            data.form_service_id ||
            metadata.pending_service_id ||
            metadata.orchestration_target_service_id ||
            orchestrationDecision.target_service_id ||
            serviceLabel ||
            "",
        ).trim();

        if (shouldShowInlineForm) {
          const values: Record<string, string> = {};
          const countryCodes: Record<string, PhoneCountryCode> = {};
          const latestProfile = activePhaseProfileRef.current;
          for (const field of fields) {
            // Priority: server-provided default_value > prefillSource lookup from profile.
            // server default_value comes from deterministic mapping or LLM extraction.
            // prefillSource is a key like "guest_name" / "guest_phone" / "room_number"
            // that resolves against the active phase profile set in the harness sidebar.
            let resolved = field.defaultValue || "";
            if (!resolved && field.prefillSource && latestProfile) {
              const profileVal = latestProfile[field.prefillSource as keyof ProfileMap];
              if (profileVal) resolved = String(profileVal);
            }
            // Fall back to the selected booking so a known guest just verifies
            // their details instead of retyping. The booking always carries the
            // guest's name/phone/room even when the phase profile doesn't (a
            // guest identified by booking rather than a lead form), so map the
            // common prefill sources here. Belt-and-suspenders on top of the
            // profile lookup above.
            if (!resolved && field.prefillSource && selectedBooking) {
              const bookingFallback: Record<string, string | undefined> = {
                room_number: selectedBooking.room_number,
                guest_name: selectedBooking.guest_name,
                guest_phone: selectedBooking.guest_phone,
                guest_email: (selectedBooking as { guest_email?: string }).guest_email,
              };
              const fb = bookingFallback[field.prefillSource];
              if (fb) resolved = String(fb);
            }
            if (isPhoneField(field)) {
              // A prefilled phone may arrive with its country code baked in
              // ("+91 9845061230", or "918858475656" with no '+'). The field has
              // its OWN country picker, so split the code out of the digits.
              const split = splitPhone(resolved);
              values[field.id] = split.digits;
              countryCodes[field.id] = split.country;
            } else {
              values[field.id] = resolved;
            }
          }
          setInlineForm({
            messageId: assistantId,
            serviceId: resolvedServiceId,
            fields,
            values,
            countryCodes,
            errors: {},
            submitting: false,
            successMessage: "",
            cancellable: formCancellable,
            cancelling: false,
          });
          setSuggestions([]);
        } else {
          setInlineForm(null);
          // Always source chips from /api/chat/suggestions so quality rules
          // are consistent and post-response generation is the single authority.
          setSuggestions([]);
          void fetchAndShowSuggestions(
            String(data.message || ""),
            text,
            assistantTurnId,
          );
        }
      } catch (error) {
        // Mark user message as failed
        if (pendingUserMsgIdRef.current) {
          updateMsgStatus(pendingUserMsgIdRef.current, "error");
        }
        const message =
          error instanceof Error
            ? `Sorry, failed to process this message (${error.message}).`
            : "Sorry, failed to process this message.";
        setMessages((prev) => [
          ...prev,
          {
            id: `a_error_${Date.now()}`,
            role: "assistant",
            content: message,
            canonicalContent: message,
            timestamp: Date.now(),
          },
        ]);
        setSuggestions([]);
      } finally {
        setIsSending(false);
        setIsTyping(false);
        // Put the cursor back for the next message — but ONLY with a mouse.
        // On a touch keyboard, re-focusing the composer re-opens the keypad:
        // the guest sends, the keypad slides down, the reply lands, and this
        // call drags the keypad straight back up over the answer they were
        // waiting to read. iPhone hid it because iOS ignores programmatic
        // focus outside a user gesture; Android honours it, which is why the
        // bug only ever showed there. A touch guest taps the composer when
        // they actually want to type.
        if (!isTouchDevice()) {
          setTimeout(() => {
            inputRef.current?.focus();
          }, 0);
        }
      }
    },
    [
      buildRequestMetadata,
      fetchAndShowSuggestions,
      hotelCode,
      isSending,
      sessionId,
      updateMsgStatus,
      updateSessionUiFromResponse,
      wsStatus,
      wsSend,
      propertyCount,
      locationManifest,
      locationChosen,
      phase,
      agentHandoverStatus,
      inlineForm,
      ctaContactOpen,
    ],
  );

  const normalizedPhaseId = useMemo(() => normalizePhaseId(phase) || "pre_booking", [phase]);

  const hasOnlyWelcomeMessage = useMemo(
    () => messages.length <= 1 && (messages[0]?.id || "").startsWith("welcome_"),
    [messages],
  );
  // Whether the guest has actually SENT a message. This is the robust "has the
  // guest started chatting?" signal — unlike hasOnlyWelcomeMessage it does not
  // depend on how many welcome/bot rows happen to be loaded at a given instant
  // (which varies by device/network timing → on some phones the intro form was
  // skipped in favour of the bare location dropdown on first load).
  const hasSentUserMessage = useMemo(
    () => messages.some((m) => m.role === "user"),
    [messages],
  );

  const ctaIsMultiProperty = propertyCount > 1 && locationManifest.length > 0;
  const ctaIsSingleProperty = propertyCount === 1;
  // Several properties in ONE location (two hotels in the same city): there is
  // no location to choose, so it counts as chosen from the start and the city
  // field and dropdown never appear. Hotels spread over several locations are
  // unaffected.
  const singleLocationLabel =
    ctaIsMultiProperty && locationManifest.length === 1 ? locationManifest[0].label : "";

  // Hide the greeting bubble ONLY when the intro form is the opener — i.e. a
  // brand-new visitor with no details on file. The form carries its own
  // greeting, so showing a bubble above it was two hellos for one action.
  //
  // A RETURNING visitor (details already frozen — `bookingContact` set) never
  // sees that form: they only get asked for a location. They must still be
  // greeted, so the bubble stays for them ("Hi Sana, welcome back — choose the
  // location..."). Same on a new conversation id, where the lead is restored.
  //
  // While the manifest is still loading we also suppress, otherwise the bubble
  // flashes in for a moment before the form resolves. If the load FAILS,
  // `businessWelcomeLoaded` still flips true and the count stays 0, so the
  // bubble comes back rather than leaving an empty panel.
  const suppressWelcomeBubble =
    normalizedPhaseId === "pre_booking" &&
    agentHandoverStatus !== "agent_active" &&
    !bookingContact &&
    (ctaIsMultiProperty || !businessWelcomeLoaded);

  // Location selection (multi-property, pre-booking):
  // - At START (only welcome message) there is NO gate — the input is typeable
  //   and only the "Book a Stay" CTA shows.
  // - Once the guest has started chatting WITHOUT choosing a location, the
  //   dropdown is shown and the input is LOCKED to selection (they can only
  //   pick a location).
  // - After they pick, the dropdown stays visible but locked.
  // Returning visitor (frozen lead, no intro form): we ask ONLY for location,
  // up front — the dropdown shows and the input is locked from the first screen
  // (not just after they type). bookingContact is set for a returning lead; a
  // brand-new visitor only gets it AFTER submitting the intro form, by which
  // point locationChosen is already true, so this never double-prompts them.
  const returningLeadNeedsLocation =
    !!bookingContact &&
    ctaIsMultiProperty &&
    !locationChosen &&
    !singleLocationLabel &&
    normalizedPhaseId === "pre_booking" &&
    agentHandoverStatus !== "agent_active" &&
    !inlineForm &&
    !ctaContactOpen;
  const locationPromptActive =
    returningLeadNeedsLocation ||
    (ctaIsMultiProperty &&
      !locationChosen &&
      !singleLocationLabel &&
      hasSentUserMessage &&
      normalizedPhaseId === "pre_booking" &&
      agentHandoverStatus !== "agent_active" &&
      !inlineForm &&
      !ctaContactOpen);
  // Input is locked while we're waiting for the location pick, AND while an
  // inline form is open/submitting — the guest must complete (or the form must
  // finish) before they can send another chat message. It re-enables once the
  // confirmation bot message arrives (the form is closed on success).
  const formAwaitingCompletion = Boolean(inlineForm && !inlineForm.successMessage);
  // Upfront mandatory intro form: at the very start of a pre-booking chat we
  // collect the guest's details + location BEFORE any chatting. Until that form
  // is submitted (bookingContact set), the chat input stays locked — there is
  // only ONE flow now (no separate "Book a Stay" CTA path).
  const introFormRequired =
    normalizedPhaseId === "pre_booking" &&
    agentHandoverStatus !== "agent_active" &&
    !bookingContact &&
    !inlineForm &&
    !hasSentUserMessage &&
    // `|| !businessWelcomeLoaded`: the property manifest call can take ~10s cold.
    // Now that the greeting bubble no longer fills that gap, gating the form on
    // it left the panel blank for the whole wait. Render the form straight away
    // instead — the city dropdown populates when the manifest lands, and the
    // city field is only required once we know it's a multi-property hotel.
    (ctaIsMultiProperty || ctaIsSingleProperty || !businessWelcomeLoaded);
  // The room-booking help form opens only when the guest taps "Get help from our
  // team"; while it is open the chat box is locked like for every other open form,
  // until it is sent or cancelled. Admin-managed hotels only (T24 onward) - the
  // others keep the behaviour they were tested with.
  const assistFormOpen = Boolean(assistForm) && prebookingConfig !== null;
  const inputGated = locationPromptActive || formAwaitingCompletion || introFormRequired || assistFormOpen;

  // Auto-open the contact form upfront so the guest sees it with the welcome
  // message and must complete it before chatting.
  // `conversationId` is a dependency so this RE-RUNS on every new conversation
  // (e.g. the dustbin "new chat" rotates it). Without it, rotating the thread
  // WITHOUT having filled the form leaves `introFormRequired` stuck at `true`
  // (no value change → effect never re-fires), yet resetConversationUi already
  // closed the form → the guest could chat without ever entering their details.
  useEffect(() => {
    if (introFormRequired) setCtaContactOpen(true);
  }, [introFormRequired, conversationId, convoResetNonce]);

  // Anchor for the per-hotel service CTAs: they ride under the newest bot
  // bubble, and only while the guest has barely started (the auto-intro counts
  // as their one message), so they disappear once a real conversation is going.
  const lastAssistantRowId = useMemo(() => {
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      if (messages[i]?.role === "assistant") return messages[i].id;
    }
    return "";
  }, [messages]);
  const userMessageCount = useMemo(
    () => messages.filter((m) => m.role === "user").length,
    [messages],
  );

  const welcomeCtaEligible =
    showWelcomeCta &&
    !isSending &&
    !inlineForm &&
    normalizedPhaseId === "pre_booking" &&
    hasOnlyWelcomeMessage &&
    // `hasOnlyWelcomeMessage` counts rows, so between the guest submitting the
    // intro form and their first message appearing there is a frame where it is
    // still true — the CTAs flashed back on under the greeting. This is the
    // robust signal: once the guest has sent anything, the opening CTAs are done.
    !hasSentUserMessage &&
    (ctaIsMultiProperty || ctaIsSingleProperty || !businessWelcomeLoaded);

  const filteredLocationOptions = useMemo(() => {
    if (!locationManifest.length) return [] as LocationManifestEntry[];
    const query = String(locationSearch || "").trim().toLowerCase();
    if (!query) return locationManifest;
    return locationManifest.filter((entry) =>
      entry.label.toLowerCase().includes(query),
    );
  }, [locationManifest, locationSearch]);

  // What an opening CTA says on the guest's behalf. Kept in ONE place because
  // two entry points need the same answer: tapping a button when we already
  // know the guest, and submitting the details form when we don't.
  //
  // A hotel that configures its own service CTAs (chat_welcome_service_ctas_json)
  // sends the guest to the agent that OWNS the topic they tapped — "Book a Stay"
  // to the room agent, which answers on room types, not to the general overview.
  // Tapping a topic button IS the guest saying what they want; making them read
  // a property summary first throws that away. Every other tenant keeps the
  // exploration phrasing exactly as before.
  const welcomeCtaPhrase = useCallback(
    (intent: "stay" | "venue", location: string) => {
      if (intent === "venue") {
        return "I'd like to enquire about hosting an event at your hotel.";
      }
      if (location) {
        return `I'm exploring ${location} — tell me about the hotels there and what makes each special.`;
      }
      if (welcomeServiceCtas.length > 0) {
        // ASK, do not announce. "I'd like to book a room" reads as booking
        // intent, and the room agent answers that with the booking button and
        // nothing else — the guest taps "Book a Stay" and is immediately asked
        // for details about a room nobody has described to them yet. Phrasing it
        // as a question makes the agent describe the rooms first and offer the
        // button after, which is what the venue button already does.
        return "What room types do you have, and what's included in each?";
      }
      return ctaIsSingleProperty
        ? "I'm exploring your hotel — tell me about it and what's special here."
        : "I'm exploring my options — tell me about your hotels and what makes each special.";
    },
    [welcomeServiceCtas, ctaIsSingleProperty],
  );

  // CTA "Book a Stay" → open the contact form first (collect name/phone/email),
  // then proceed to location selection (multi) or booking (single).
  const handleWelcomeCtaClick = useCallback((intent: "stay" | "venue" = "stay") => {
    setWelcomeCtaIntent(intent);
    setLocationPickerOpen(false);
    setCtaContactErrors({});
    // We already have this guest's details, so asking for them again is a dead
    // end — the form came back pre-filled with what they typed a minute ago and
    // stood between them and the answer they asked for. Hand the topic straight
    // to its agent instead. Gated on the per-hotel CTA config so tenants whose
    // CTA still has to collect details (and a city) are untouched.
    if (welcomeServiceCtas.length > 0 && bookingContact) {
      setCtaContactOpen(false);
      void sendMessage(welcomeCtaPhrase(intent, ""), "cta_book_stay");
      return;
    }
    setCtaContactOpen((open) => !open);
  }, [welcomeServiceCtas, bookingContact, welcomeCtaPhrase, sendMessage]);

  // Admin-configured opening button (Bot Training -> Pre-booking).
  const runAdminWelcomeButton = useCallback(
    (button: AdminWelcomeButton, location = "") => {
      if (button.action === "intro") {
        // The label is what the guest sees they asked; the backend shows the
        // saved message for their location (with one hotel, or several hotels
        // in one place, there is only one location to show).
        const where = location || chosenLocationLabel || singleLocationLabel;
        void sendMessage(button.label, "welcome_cta", {
          show_location_intro: true,
          ...(where ? { intro_city: where } : {}),
        });
        return;
      }
      void sendMessage(button.message || button.label, "welcome_cta");
    },
    [chosenLocationLabel, singleLocationLabel, sendMessage],
  );

  const handleAdminWelcomeButton = useCallback(
    (button: AdminWelcomeButton) => {
      setLocationPickerOpen(false);
      setCtaContactErrors({});
      // Without the guest's details yet, the lead form comes first and the
      // button runs once it is submitted.
      if (button.action === "intro" && !bookingContact) {
        pendingWelcomeButtonRef.current = button;
        setCtaContactOpen(true);
        return;
      }
      runAdminWelcomeButton(button);
    },
    [bookingContact, runAdminWelcomeButton],
  );

  const handleCtaContactSubmit = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      const name = ctaContactValues.name.trim();
      const phoneDigits = ctaContactValues.phone.replace(/\D/g, "");
      const email = ctaContactValues.email.trim();
      // One location for every property: that is where they are staying.
      const location = ctaContactValues.location.trim() || singleLocationLabel;
      const errors: Record<string, string> = {};
      // Validate only the fields this hotel actually renders, and only demand
      // the ones marked required in Admin > Forms. A field that was removed
      // there must not block submission from a rule it no longer has.
      const shownFields = leadFormFields.filter(
        (f) => f.fieldType !== "CITY" || (ctaIsMultiProperty && !singleLocationLabel),
      );
      const fieldFor = (t: LeadFieldType) => shownFields.find((f) => f.fieldType === t);

      const nameField = fieldFor("FULL_NAME");
      if (nameField) {
        if (!name) {
          if (nameField.required) errors.name = "Please enter your name.";
        } else {
          // Same person-name rule the support/ticket inline form enforces, so the
          // opening lead form can't accept a name the ticket form later rejects
          // (e.g. "'1"). Keeps validation consistent across both forms.
          const nameErr = validateNameInput(name);
          if (nameErr) errors.name = nameErr;
        }
      }

      const phoneField = fieldFor("MOBILE_NUMBER") || fieldFor("ALTERNATE_MOBILE_NUMBER");
      if (phoneField) {
        if (!phoneDigits) {
          if (phoneField.required) errors.phone = "Please enter your phone number.";
        } else {
          const phoneError = validatePhone(phoneDigits, ctaContactPhoneCode);
          if (phoneError) errors.phone = phoneError;
        }
      }

      const emailField = fieldFor("EMAIL");
      if (emailField) {
        if (!email) {
          if (emailField.required) errors.email = "Please enter your email.";
        } else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
          errors.email = "Please enter a valid email.";
        }
      }

      const cityField = fieldFor("CITY");
      if (cityField && cityField.required && !location) {
        errors.location = "Please choose the city you want to stay in.";
      }

      for (const field of shownFields) {
        if (["FULL_NAME", "EMAIL", "MOBILE_NUMBER", "ALTERNATE_MOBILE_NUMBER", "CITY"].includes(field.fieldType)) {
          continue;
        }
        const slot = `custom:${field.label}`;
        if (field.required && !String(ctaContactValues.extras?.[slot] || "").trim()) {
          errors[slot] = `Please fill in ${field.label.toLowerCase()}.`;
        }
      }
      if (Object.keys(errors).length > 0) {
        setCtaContactErrors(errors);
        return;
      }
      const phone = toE164(phoneDigits, ctaContactPhoneCode);
      setBookingContact({ guest_name: name, guest_phone: phone, guest_email: email });
      setCtaContactErrors({});
      setCtaContactOpen(false);
      if (location) {
        setLocationChosen(true);
        setChosenLocationLabel(location);
      }

      // Proceed straight into the room-types conversation for that location —
      // do this FIRST so the chat responds instantly.
      // A single-property tenant has no "hotels" to compare — the plural copy
      // ("your hotels… what makes each special") reads as if there were several
      // and pulls the answer towards a group overview. Ask about THIS hotel.
      // A hotel with its own service CTAs lets the guest pick the topic AFTER
      // the details form: the buttons sit under the greeting and they start the
      // conversation. Auto-sending an overview here did two wrong things at
      // once — it pre-empted that choice, and because the buttons only show
      // before the guest's first message, it hid them for good.
      if (prebookingConfig) {
        // Admin-managed hotel: the opening button that brought them to the
        // form runs now. Otherwise the hotel chose what follows the form - the
        // saved message for their location (with one button per hotel when it
        // has several), or the opening buttons under the greeting.
        const pendingButton = pendingWelcomeButtonRef.current;
        pendingWelcomeButtonRef.current = null;
        if (pendingButton) {
          runAdminWelcomeButton(pendingButton, location);
        } else if (prebookingConfig.after_lead_form === "intro") {
          void sendMessage(welcomeCtaPhrase("stay", location), "cta_book_stay", {
            show_location_intro: true,
            ...(location ? { intro_city: location } : {}),
          });
        }
      } else if (welcomeServiceCtas.length === 0) {
        void sendMessage(welcomeCtaPhrase(welcomeCtaIntent, location), "cta_book_stay");
      }

      // Silently create the booking lead in the BACKGROUND (fire-and-forget).
      // The external ticketing API can be slow/unavailable, so we must NOT block
      // the chat on it. Capture the ticket id when it resolves so later turns
      // update THIS lead instead of creating a duplicate.
      void (async () => {
        try {
          const leadResp = await fetch("/api/chat/form-submit", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              session_id: sessionId,
              hotel_code: hotelCode,
              service_id: BOOKING_LEAD_SERVICE_ID,
              form_data: { full_name: name, phone, email, ...(location ? { location } : {}) },
              metadata: {
                ...buildRequestMetadata(),
                guest_name: name,
                guest_phone: phone,
                guest_email: email,
                lead_source: "cta_book_stay",
                ...(location ? { booking_location: location } : {}),
              },
            }),
          });
          const leadData = (await leadResp.json()) as { ticket_id?: string };
          if (leadData?.ticket_id) setBookingLeadId(String(leadData.ticket_id));
        } catch {
          /* lead creation is best-effort */
        }
      })();
    },
    // `welcomeCtaIntent` was missing here, so this callback closed over its
    // FIRST value ("stay") forever: tapping the venue button set the state but
    // the submit handler never saw it, and every guest who asked about events
    // was answered with a room pitch.
    [ctaContactValues, ctaContactPhoneCode, ctaIsMultiProperty, sessionId, hotelCode,
     leadFormFields,
     buildRequestMetadata, sendMessage, welcomeCtaIntent, welcomeCtaPhrase, welcomeServiceCtas,
     prebookingConfig, singleLocationLabel, runAdminWelcomeButton],
  );

  const handleLocationPickerSelect = useCallback(
    (entry: LocationManifestEntry) => {
      const label = String(entry.label || "").trim();
      if (!label) return;
      const noun = entry.property_names.length > 1 ? "properties" : "property";
      // In the CTA booking flow (contact already collected) the guest wants to
      // book — take them straight to room types & offers. In normal chat, a
      // location pick is an intro request.
      setLocationChosen(true);
      setChosenLocationLabel(label);
      setLocationPickerOpen(false);
      // Picking a location is an EXPLORE intent, not a booking — mirror the
      // new-session flow's phrasing so a returning lead gets the same "tell me
      // about the hotels here" intro instead of being shoved straight into a
      // room-booking ask. (`noun` still used for the non-lead fallback.)
      const phrase = bookingContact
        ? `I'm exploring ${label} — tell me about the hotels there and what makes each special.`
        : `Tell me more about the ${label} ${noun}`;
      void sendMessage(phrase, "cta_location_picker");
    },
    [bookingContact, sendMessage],
  );

  const handleInlineFormSubmit = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      if (!inlineForm || !inlineForm.serviceId || inlineForm.submitting || inlineForm.successMessage) return;

      const composedValues: Record<string, string> = {};
      const requiredErrors: Record<string, string> = {};
      for (const field of inlineForm.fields) {
        const rawValue = String(inlineForm.values[field.id] || "").trim();
        if (field.required && !rawValue) {
          requiredErrors[field.id] = buildInlineRequiredFieldError(field);
        }
        if (isPhoneField(field) && rawValue) {
          const country = inlineForm.countryCodes[field.id] || DEFAULT_PHONE_COUNTRY;
          const phoneErr = validatePhone(rawValue, country);
          if (phoneErr) {
            requiredErrors[field.id] = phoneErr;
            continue;
          }
          composedValues[field.id] = toE164(rawValue, country);
          continue;
        }
        if (isNameField(field) && rawValue) {
          const nameErr = validateNameInput(rawValue);
          if (nameErr) {
            requiredErrors[field.id] = nameErr;
            continue;
          }
        }
        composedValues[field.id] = rawValue;
      }

      if (Object.keys(requiredErrors).length > 0) {
        setInlineForm((prev) =>
          prev
            ? {
                ...prev,
                submitting: false,
                errors: requiredErrors,
              }
            : prev,
        );
        return;
      }

      setInlineForm((prev) =>
        prev
          ? {
              ...prev,
              submitting: true,
              errors: {},
            }
          : prev,
      );

      // Show form values as a user message in the conversation history
      const formSummaryLines = inlineForm.fields
        .map((field) => {
          const val = String(composedValues[field.id] || "").trim();
          return val ? `${field.label}: ${val}` : null;
        })
        .filter(Boolean);
      if (formSummaryLines.length > 0) {
        const formMsgId = `user-form-${Date.now()}`;
        const formMsgText = formSummaryLines.join("\n");
        setMessages((prev) => [
          ...prev,
          { id: formMsgId, role: "user", content: formMsgText, canonicalContent: formMsgText, timestamp: Date.now() },
        ]);
      }

      try {
        const response = await fetch("/api/chat/form-submit", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            session_id: sessionId,
            hotel_code: hotelCode,
            service_id: inlineForm.serviceId,
            form_data: composedValues,
            metadata: buildRequestMetadata(),
          }),
        });
        const result = (await response.json()) as {
          success?: boolean;
          message?: string;
          closer?: string;
          ticket_id?: string;
          booking_cta?: { url?: string; label?: string } | null;
          errors?: Array<{ id?: string; field_id?: string; message?: string }>;
        };

        if (result.success) {
          // Retire the event button once its enquiry is actually in.
          if (/banquet|event/i.test(String(inlineForm.serviceId || ""))) {
            setEventSubmittedThisConvo(true);
          }
          // Show the confirmation as a normal BOT MESSAGE in the thread (not
          // tucked inside the form box). The ack ("we've received your request…")
          // becomes its own assistant bubble, followed by the
          // closer ("want to explore more about X…") as a separate bubble.
          const ackText = String(result.message || "Submitted successfully.").trim();
          const closerText = String(result.closer || "").trim();
          const bookingCta =
            result.booking_cta && String(result.booking_cta.url || "").trim()
              ? { url: String(result.booking_cta.url), label: String(result.booking_cta.label || "Book on website") }
              : null;
          const ts = Date.now();
          const newBubbles: Message[] = [];
          if (ackText) {
            newBubbles.push({
              id: `assistant-${ts}-ack`,
              role: "assistant",
              content: ackText,
              canonicalContent: ackText,
              raw: {
                message: ackText,
                source: "form_submit_ack",
                ...(bookingCta && !closerText ? { metadata: { booking_cta: bookingCta } } : {}),
              } as unknown as ChatApiResponse,
            });
          }
          if (closerText || bookingCta) {
            newBubbles.push({
              id: `assistant-${ts}-closer`,
              role: "assistant",
              content: closerText,
              canonicalContent: closerText,
              raw: {
                message: closerText,
                source: "form_submit_closer",
                ...(bookingCta ? { metadata: { booking_cta: bookingCta } } : {}),
              } as unknown as ChatApiResponse,
            });
          }
          if (newBubbles.length > 0) {
            setMessages((prev) => [...prev, ...newBubbles]);
          }
          // Close the form — the acknowledgement now lives in the thread, and
          // closing it re-enables the chat input (which was locked while the
          // form was open / submitting).
          setInlineForm(null);
          setSessionState("completed");
          setTicketStatus({
            label: `Created: ${String(result.ticket_id || "unknown-id")}`,
            badge: "created",
          });
          setMessageCount((prev) => prev + 1);
          setSuggestions([]);
          return;
        }

        const errors: Record<string, string> = {};
        if (Array.isArray(result.errors) && result.errors.length > 0) {
          for (const row of result.errors) {
            const key = String(row.field_id || row.id || "").trim();
            if (!key) continue;
            errors[key] = String(row.message || "Please correct this field.");
          }
        } else {
          errors._global = String(result.message || "Submission failed. Please try again.");
        }
        setInlineForm((prev) =>
          prev
            ? {
                ...prev,
                submitting: false,
                errors,
              }
            : prev,
        );
      } catch {
        setInlineForm((prev) =>
          prev
            ? {
                ...prev,
                submitting: false,
                errors: { _global: "Network error. Please try again." },
              }
            : prev,
        );
      }
    },
    [buildRequestMetadata, hotelCode, inlineForm, sessionId],
  );

  // Back out of an open form. An open form switches the text box OFF, so
  // without this the guest's only exits are submitting a request they don't
  // want or abandoning the chat. The server call is the important half: the
  // turn that opened the form parked the session on
  // pending_action='collect_form_details', and closing the form here alone
  // would leave that parked — the next typed message then routes ungrounded.
  const handleInlineFormCancel = useCallback(async () => {
    if (!inlineForm || inlineForm.submitting || inlineForm.cancelling || inlineForm.successMessage) return;
    const serviceId = inlineForm.serviceId;
    setInlineForm((prev) => (prev ? { ...prev, cancelling: true, errors: {} } : prev));

    const ts = Date.now();
    const userText = "Cancel this request";
    const newBubbles: ChatMessageRow[] = [
      { id: `user-form-cancel-${ts}`, role: "user", content: userText, canonicalContent: userText, timestamp: ts },
    ];

    let ackText = "No problem, I have cancelled that request. Is there anything else I can help you with?";
    try {
      const response = await fetch("/api/chat/form-cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: sessionId,
          hotel_code: hotelCode,
          service_id: serviceId,
          metadata: buildRequestMetadata(),
        }),
      });
      const result = (await response.json()) as { success?: boolean; message?: string };
      if (result.success && String(result.message || "").trim()) {
        ackText = String(result.message).trim();
      }
    } catch {
      // Deliberately swallowed: the form still closes below. Leaving the guest
      // locked out of the text box because a network call failed is the exact
      // trap this button exists to remove.
    }

    newBubbles.push({
      id: `assistant-${ts}-form-cancel`,
      role: "assistant",
      content: ackText,
      canonicalContent: ackText,
      raw: { message: ackText, source: "form_cancel_ack" } as unknown as ChatApiResponse,
    });
    setMessages((prev) => [...prev, ...newBubbles]);
    // Closing the form is what re-enables the chat input (it was locked by
    // `formAwaitingCompletion` for as long as the form was open).
    setInlineForm(null);
    setSessionState("idle");
    setSuggestions([]);
    setMessageCount((prev) => prev + 1);
  }, [buildRequestMetadata, hotelCode, inlineForm, sessionId]);

  const handleResetSession = useCallback(async () => {
    try {
      const response = await fetch(`/api/chat/session/${encodeURIComponent(sessionId)}/reset`, {
        method: "POST",
      });
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      setSessionState("idle");
      setTicketStatus({ label: "No ticket action", badge: "idle" });
      setTicketDetails(null);
      setInlineForm(null);
      setSuggestions([]);
      setMessages([
        {
          id: `welcome_${Date.now()}`,
          role: "assistant",
          content: resolvedWelcomeMessage,
          canonicalContent: resolvedWelcomeMessage,
          suggested_actions: welcomeSuggestedActions,
          timestamp: Date.now(),
        },
      ]);
    } catch {
      setMessages((prev) => [
        ...prev,
        {
          id: `a_reset_error_${Date.now()}`,
          role: "assistant",
          content: "Failed to reset the session.",
          canonicalContent: "Failed to reset the session.",
          timestamp: Date.now(),
        },
      ]);
    }
  }, [resolvedWelcomeMessage, sessionId]);

  const handleLoadHistory = useCallback(async () => {
    setHistoryOpen(true);
    setHistoryLoading(true);
    setHistoryError("");
    try {
      const response = await fetch(`/api/chat/session/${encodeURIComponent(sessionId)}`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = (await response.json()) as SessionHistory;
      setHistoryData(payload);
    } catch (error) {
      setHistoryData(null);
      setHistoryError(error instanceof Error ? error.message : "Failed to load history");
    } finally {
      setHistoryLoading(false);
    }
  }, [sessionId]);

  const handleLoadTickets = useCallback(async () => {
    setTicketsOpen(true);
    setTicketsLoading(true);
    setTicketsError("");
    try {
      const response = await fetch("/admin/api/tickets");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = (await response.json()) as { tickets?: TicketRow[] };
      setTickets(Array.isArray(payload.tickets) ? payload.tickets : []);
    } catch (error) {
      setTickets([]);
      setTicketsError(error instanceof Error ? error.message : "Failed to load tickets");
    } finally {
      setTicketsLoading(false);
    }
  }, []);

  const handleCreateBooking = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      setBookingDraftSubmitting(true);
      setBookingDraftError("");

      const body = {
        ...bookingDraft,
        num_guests: Number(bookingDraft.num_guests || "1") || 1,
      };

      try {
        if (!activeHotelCode) {
          throw new Error("Please select a property before creating a booking.");
        }
        const response = await fetch(`/admin/api/bookings?hotel_code=${encodeURIComponent(activeHotelCode)}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!response.ok) {
          const payload = await response.json().catch(() => ({}));
          throw new Error(String((payload as Record<string, unknown>).detail || response.statusText || "Failed"));
        }
        setBookingModalOpen(false);
        setBookingDraft(initialBookingDraft);
        await loadBookings();
      } catch (error) {
        setBookingDraftError(error instanceof Error ? error.message : "Failed to create booking");
      } finally {
        setBookingDraftSubmitting(false);
      }
    },
    [activeHotelCode, bookingDraft, loadBookings],
  );

  const profileStatusText = useMemo(() => {
    const phaseId = normalizePhaseId(phase) || "unknown";
    if (!autoPhaseProfile) return `Auto profile mapping is OFF for ${phaseId}.`;
    if (!activePhaseProfile) return `No mapped test profile for ${phaseId}.`;
    return `Mapped test profile active for ${phaseId}.`;
  }, [activePhaseProfile, autoPhaseProfile, phase]);

  const bookingDetailsText = useMemo(() => {
    if (!selectedBooking) return "No booking selected.";
    return [
      `Code: ${selectedBooking.confirmation_code || "-"}`,
      `Guest: ${selectedBooking.guest_name || "-"} (${selectedBooking.guest_phone || "-"})`,
      `Property: ${selectedBooking.property_name || "-"}`,
      `Room: ${selectedBooking.room_number || "-"} (${selectedBooking.room_type || "-"})`,
      `Dates: ${selectedBooking.check_in_date || "-"} to ${selectedBooking.check_out_date || "-"}`,
      `Status: ${selectedBooking.status || "-"} | Phase: ${selectedBooking.phase || "-"}`,
    ].join("\n");
  }, [selectedBooking]);

  const handleAssistFormSubmit = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      if (!assistForm || assistForm.submitting) return;

      const errors: Record<string, string> = {};
      if (!assistForm.checkIn) errors.checkIn = "Please pick your check-in date.";
      if (!assistForm.checkOut) errors.checkOut = "Please pick your check-out date.";
      if (assistForm.checkIn && assistForm.checkOut && assistForm.checkOut <= assistForm.checkIn) {
        errors.checkOut = "Check-out must be after check-in.";
      }
      const guestCount = Number(assistForm.guests);
      if (!assistForm.guests || !Number.isFinite(guestCount) || guestCount < 1) {
        errors.guests = "Please enter how many guests.";
      }
      if (Object.keys(errors).length > 0) {
        setAssistForm((prev) => (prev ? { ...prev, errors } : prev));
        return;
      }

      setAssistForm((prev) => (prev ? { ...prev, submitting: true, errors: {} } : prev));
      const submittedFor = assistForm.messageId;
      try {
        const response = await fetch("/api/chat/booking-assistance", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            session_id: sessionId,
            hotel_code: hotelCode,
            property_name: assistForm.propertyName,
            // Send the guest's own details too: staff have to CALL them back,
            // and the ticket was landing with no contact context.
            guest_name: bookingContact?.guest_name || "",
            guest_phone: bookingContact?.guest_phone || "",
            guest_email: bookingContact?.guest_email || "",
            check_in: assistForm.checkIn,
            check_out: assistForm.checkOut,
            guests: assistForm.guests,
            message: assistForm.message,
            metadata: buildRequestMetadata(),
          }),
        });
        const result = (await response.json()) as { success?: boolean; message?: string };
        if (!result.success) {
          setAssistForm((prev) =>
            prev
              ? {
                  ...prev,
                  submitting: false,
                  errors: { _global: String(result.message || "Submission failed. Please try again.") },
                }
              : prev,
          );
          return;
        }
        const ack = String(result.message || "Thanks - our team will be in touch.").trim();
        setAssistDoneIds((prev) => new Set(prev).add(submittedFor));
        setAssistSubmittedThisConvo(true);
        setAssistForm(null);
        setMessages((prev) => [
          ...prev,
          {
            id: `assistant-assist-${Date.now()}`,
            role: "assistant",
            content: ack,
            canonicalContent: ack,
            timestamp: Date.now(),
          },
        ]);
      } catch {
        setAssistForm((prev) =>
          prev
            ? { ...prev, submitting: false, errors: { _global: "Network error. Please try again." } }
            : prev,
        );
      }
    },
    [assistForm, bookingContact, buildRequestMetadata, hotelCode, sessionId],
  );

  const renderAssistForm = () => {
    if (!assistForm) return null;
    const fieldBase =
      "w-full min-w-0 max-w-full box-border rounded-lg border bg-white px-3 py-2 text-sm outline-none transition focus:ring-2";
    const labelCls = "mb-1 block text-[11px] font-semibold uppercase tracking-wide text-slate-500";
    const border = (bad?: string) => ({
      borderColor: bad ? "#dc2626" : `${widgetUiColor}40`,
      color: widgetTextColor,
      ["--tw-ring-color" as string]: `${widgetUiColor}55`,
    });
    const today = new Date().toISOString().slice(0, 10);
    return (
      <form
        onSubmit={handleAssistFormSubmit}
        className="mt-2 space-y-3 rounded-2xl border bg-white p-3 shadow-sm"
        style={{ borderColor: `${widgetUiColor}26` }}
      >
        <div className="text-sm font-semibold" style={{ color: widgetUiColor }}>
          Let our team help you book
        </div>
        <div className="text-[11px] text-slate-500">
          Share your dates and we will pass this to the team - they will call you with the best
          available rates.
        </div>
        {assistForm.errors._global ? (
          <p className="text-[11px] text-red-600">{assistForm.errors._global}</p>
        ) : null}
        {/* Check-in and check-out sat side by side in a plain flex row. A
            flex child defaults to min-width:auto, so neither column could
            shrink below the native date control's own width — on a phone the
            pair was wider than the panel and check-out hung off the edge.
            They now stack on a narrow panel and only sit side by side when
            there is room, and each column may shrink. */}
        <div className="flex flex-col gap-2 sm:flex-row">
          <div className="min-w-0 flex-1">
            <label className={labelCls}>Check-in</label>
            <input
              type="date"
              min={today}
              value={assistForm.checkIn}
              onChange={(e) => setAssistForm((prev) => (prev ? { ...prev, checkIn: e.target.value } : prev))}
              className={fieldBase}
              style={{ ...border(assistForm.errors.checkIn), WebkitAppearance: "none", appearance: "none", minWidth: 0, maxWidth: "100%" }}
            />
            {assistForm.errors.checkIn ? (
              <p className="mt-1 text-[11px] text-red-600">{assistForm.errors.checkIn}</p>
            ) : null}
          </div>
          <div className="min-w-0 flex-1">
            <label className={labelCls}>Check-out</label>
            <input
              type="date"
              min={assistForm.checkIn || today}
              value={assistForm.checkOut}
              onChange={(e) => setAssistForm((prev) => (prev ? { ...prev, checkOut: e.target.value } : prev))}
              className={fieldBase}
              style={{ ...border(assistForm.errors.checkOut), WebkitAppearance: "none", appearance: "none", minWidth: 0, maxWidth: "100%" }}
            />
            {assistForm.errors.checkOut ? (
              <p className="mt-1 text-[11px] text-red-600">{assistForm.errors.checkOut}</p>
            ) : null}
          </div>
        </div>
        <div>
          <label className={labelCls}>Number of guests</label>
          <input
            type="number"
            min={1}
            max={99}
            inputMode="numeric"
            value={assistForm.guests}
            onChange={(e) =>
              setAssistForm((prev) =>
                prev ? { ...prev, guests: e.target.value.replace(/[^0-9]/g, "").slice(0, 2) } : prev,
              )
            }
            placeholder="e.g. 2"
            className={fieldBase}
            style={border(assistForm.errors.guests)}
          />
          {assistForm.errors.guests ? (
            <p className="mt-1 text-[11px] text-red-600">{assistForm.errors.guests}</p>
          ) : null}
        </div>
        <div>
          <label className={labelCls}>Anything else? (optional)</label>
          <textarea
            rows={3}
            value={assistForm.message}
            onChange={(e) => setAssistForm((prev) => (prev ? { ...prev, message: e.target.value } : prev))}
            placeholder="Room preference, occasion, arrival time..."
            className={fieldBase}
            style={border()}
          />
        </div>
        <div className="flex gap-2">
          <button
            type="submit"
            disabled={assistForm.submitting}
            className="flex-1 rounded-full px-4 py-2 text-sm font-medium text-white shadow-sm transition-opacity hover:opacity-90 disabled:opacity-60"
            style={{ backgroundColor: widgetUiColor }}
          >
            {assistForm.submitting ? "Sending..." : "Send to the team"}
          </button>
          <button
            type="button"
            onClick={() => setAssistForm(null)}
            className="rounded-full border px-4 py-2 text-sm transition-colors hover:bg-slate-50"
            style={{ borderColor: `${widgetUiColor}40`, color: widgetUiColor }}
          >
            Cancel
          </button>
        </div>
      </form>
    );
  };

  const renderInlineForm = () => {
    if (!inlineForm) return null;
    // Stay-date bounds normally come from the selected booking, but a test
    // PROFILE (e.g. Sana1 pre_checkin — sourced from GHN guest-info, not a
    // booking row) carries its check-in/out on activePhaseProfile. Fall back to
    // it so the date picker reflects the guest's real stay window.
    const boundsBooking: BookingRow | null =
      selectedBooking ??
      (activePhaseProfile &&
      (activePhaseProfile.check_in_date || activePhaseProfile.check_out_date)
        ? ({
            check_in_date: String(activePhaseProfile.check_in_date || ""),
            check_out_date: String(activePhaseProfile.check_out_date || ""),
          } as BookingRow)
        : null);
    const bounds = getStayDateBounds(phase, boundsBooking);
    return (
      <form
        onSubmit={handleInlineFormSubmit}
        className="mt-3 rounded-xl p-3"
        style={{ backgroundColor: `${widgetUiColor}08`, border: `1px solid ${widgetUiColor}20` }}
      >
        {inlineForm.successMessage ? (
          <div
            className="rounded-lg px-3 py-2 text-sm"
            style={{ backgroundColor: `${widgetUiColor}12`, color: widgetUiColor, border: `1px solid ${widgetUiColor}25` }}
          >
            {inlineForm.successMessage}
          </div>
        ) : (
          <>
            {inlineForm.errors._global ? (
              <div className="mb-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
                {inlineForm.errors._global}
              </div>
            ) : null}
            {(() => {
              const FIELDS_PER_STEP = 2;
              const totalSteps = isMobile ? Math.ceil(inlineForm.fields.length / FIELDS_PER_STEP) : 1;
              const visibleFields = isMobile
                ? inlineForm.fields.slice(mobileFormStep * FIELDS_PER_STEP, (mobileFormStep + 1) * FIELDS_PER_STEP)
                : inlineForm.fields;
              const isLastStep = !isMobile || mobileFormStep >= totalSteps - 1;
              return (
                <>
                  {isMobile && totalSteps > 1 ? (
                    <div className="mb-2 text-center text-[11px]" style={{ color: `${widgetUiColor}99` }}>
                      Step {mobileFormStep + 1} of {totalSteps}
                    </div>
                  ) : null}
                  <div className="grid grid-cols-1 gap-2">
                    {visibleFields.map((field) => {
                const fieldError = inlineForm.errors[field.id];
                // `min-w-0 max-w-full box-border` is what keeps a field inside
                // the form on a phone. A flex item defaults to min-width:auto,
                // so it refuses to shrink below its intrinsic width — and
                // `input[type=date]` carries a wide intrinsic width from its
                // built-in dd/mm/yyyy spinner parts. With w-full alone the
                // check-in / check-out boxes pushed straight out of the form.
                const commonClass = `w-full min-w-0 max-w-full box-border rounded-lg border px-2.5 py-1.5 text-sm outline-none transition-colors ${
                  fieldError
                    ? "border-rose-400 bg-rose-50"
                    : "bg-white"
                }`;
                const commonStyle: React.CSSProperties = fieldError
                  ? {}
                  : { borderColor: `${widgetUiColor}30` };
                const commonFocusHandler = (e: React.FocusEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => {
                  if (!fieldError) e.currentTarget.style.borderColor = widgetUiColor;
                };
                const commonBlurHandler = (e: React.FocusEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) => {
                  if (!fieldError) e.currentTarget.style.borderColor = `${widgetUiColor}30`;
                };
                const key = `inline_${field.id}`;

                if (field.type === "textarea") {
                  return (
                    <label key={key} className="flex flex-col gap-1">
                      <span className="text-xs font-medium" style={{ color: widgetTextColor }}>
                        {field.label}
                        {field.required ? <span style={{ color: widgetUiColor }}> *</span> : ""}
                      </span>
                      <textarea
                        className={commonClass}
                        style={commonStyle}
                        onFocus={commonFocusHandler as any}
                        onBlur={commonBlurHandler as any}
                        required={field.required}
                        rows={3}
                        value={inlineForm.values[field.id] || ""}
                        onChange={(event) => {
                          const value = event.target.value;
                          setInlineForm((prev) =>
                            prev
                              ? {
                                  ...prev,
                                  values: { ...prev.values, [field.id]: value },
                                  errors: { ...prev.errors, [field.id]: "" },
                                }
                              : prev,
                          );
                        }}
                      />
                      {fieldError ? <span className="text-xs text-rose-600">{fieldError}</span> : null}
                    </label>
                  );
                }

                if (isPhoneField(field)) {
                  const activeCountry = inlineForm.countryCodes[field.id] || DEFAULT_PHONE_COUNTRY;
                  const helperText = formatPhonePreview(inlineForm.values[field.id], activeCountry);
                  return (
                    <label key={key} className="flex flex-col gap-1">
                      <span className="text-xs font-medium" style={{ color: widgetTextColor }}>
                        {field.label}
                        {field.required ? <span style={{ color: widgetUiColor }}> *</span> : ""}
                      </span>
                      <div className="flex gap-2">
                        <PhoneCountryPicker
                          value={activeCountry}
                          accentColor={widgetUiColor}
                          className="flex h-full items-center gap-1 whitespace-nowrap rounded-lg border bg-white px-2 py-1.5 text-sm outline-none transition-colors"
                          style={{ borderColor: `${widgetUiColor}30` }}
                          onChange={(country) => {
                            // Keep the typed digits when the guest switches country —
                            // they're re-validated against the new country on blur/submit,
                            // and re-capped here because the new plan may be shorter.
                            setInlineForm((prev) =>
                              prev
                                ? {
                                    ...prev,
                                    countryCodes: { ...prev.countryCodes, [field.id]: country },
                                    values: {
                                      ...prev.values,
                                      [field.id]: clampPhoneDigits(prev.values[field.id], country),
                                    },
                                    errors: { ...prev.errors, [field.id]: "" },
                                  }
                                : prev,
                            );
                          }}
                        />
                        <input
                          type="tel"
                          inputMode="numeric"
                          className={commonClass}
                          style={commonStyle}
                          onFocus={commonFocusHandler}
                          onBlur={(event) => {
                            // Validate as soon as the guest leaves the field —
                            // immediate feedback instead of only at submit.
                            const raw = String(inlineForm.values[field.id] || "").trim();
                            const phoneErr = raw ? validatePhone(raw, activeCountry) || "" : "";
                            setInlineForm((prev) =>
                              prev
                                ? { ...prev, errors: { ...prev.errors, [field.id]: phoneErr } }
                                : prev,
                            );
                            if (!phoneErr) commonBlurHandler(event);
                          }}
                          required={field.required}
                          maxLength={PHONE_MAX_DIGITS}
                          value={inlineForm.values[field.id] || ""}
                          onChange={(event) => {
                            // Capped at what this country's plan allows, so the
                            // 11th Indian digit never lands in the field.
                            const digits = clampPhoneDigits(event.target.value, activeCountry);
                            setInlineForm((prev) =>
                              prev
                                ? {
                                    ...prev,
                                    values: { ...prev.values, [field.id]: digits },
                                    errors: { ...prev.errors, [field.id]: "" },
                                  }
                                : prev,
                            );
                          }}
                        />
                      </div>
                      <span className="text-[10px]" style={{ color: `${widgetTextColor}80` }}>{helperText}</span>
                      {fieldError ? <span className="text-xs text-rose-600">{fieldError}</span> : null}
                    </label>
                  );
                }

                if (field.type === "date") {
                  return (
                    <label key={key} className="flex min-w-0 flex-col gap-1">
                      <span className="text-xs font-medium" style={{ color: widgetTextColor }}>
                        {field.label}
                        {field.required ? <span style={{ color: widgetUiColor }}> *</span> : ""}
                      </span>
                      <input
                        type="date"
                        className={commonClass}
                        // iOS/Android render the native date control at its own
                        // intrinsic width and ignore the class width unless the
                        // platform appearance is dropped first.
                        style={{
                          ...commonStyle,
                          WebkitAppearance: "none",
                          appearance: "none",
                          minWidth: 0,
                          maxWidth: "100%",
                        }}
                        onFocus={commonFocusHandler}
                        onBlur={commonBlurHandler}
                        required={field.required}
                        min={bounds.min || undefined}
                        max={bounds.max || undefined}
                        value={inlineForm.values[field.id] || ""}
                        onChange={(event) => {
                          const value = event.target.value;
                          // Validate the moment a date is picked, not at submit.
                          const dateErr = validateStayDate(value, bounds) || "";
                          setInlineForm((prev) =>
                            prev
                              ? {
                                  ...prev,
                                  values: { ...prev.values, [field.id]: value },
                                  errors: { ...prev.errors, [field.id]: dateErr },
                                }
                              : prev,
                          );
                        }}
                      />
                      {fieldError ? <span className="text-xs text-rose-600">{fieldError}</span> : null}
                    </label>
                  );
                }

                if (field.type === "time") {
                  // When the field carries deterministic slot config (Sarovar
                  // table/spa), render an easy dropdown of allowed slots — filtered
                  // by another field's value (e.g. the chosen outlet) when
                  // slotSourceField is set. Otherwise fall back to the native time
                  // input so every other hotel's time fields stay unchanged.
                  const hasSlotConfig = Boolean(
                    field.slotHours || (field.slotSourceField && field.slotHoursMap),
                  );
                  if (hasSlotConfig) {
                    let range: [string, string] | null = null;
                    let awaitingSource = false;
                    if (field.slotSourceField) {
                      const chosen = String(inlineForm.values[field.slotSourceField] || "").trim();
                      if (!chosen) {
                        awaitingSource = true;
                      } else if (field.slotHoursMap) {
                        const matchKey = Object.keys(field.slotHoursMap).find(
                          (k) => k.toLowerCase() === chosen.toLowerCase(),
                        );
                        if (matchKey) range = field.slotHoursMap[matchKey];
                      }
                    }
                    if (!range && field.slotHours) range = field.slotHours;
                    const slots = range
                      ? generateTimeSlots(range[0], range[1], field.slotStepMinutes || 30)
                      : [];
                    const currentVal = String(inlineForm.values[field.id] || "");
                    const valInSlots = slots.some((s) => s.value === currentVal);
                    const sourceLabel = field.slotSourceField
                      ? inlineForm.fields.find((f) => f.id === field.slotSourceField)?.label ||
                        "the option above"
                      : "";
                    return (
                      <label key={key} className="flex flex-col gap-1">
                        <span className="text-xs font-medium" style={{ color: widgetTextColor }}>
                          {field.label}
                          {field.required ? <span style={{ color: widgetUiColor }}> *</span> : ""}
                        </span>
                        <select
                          className={commonClass}
                          style={commonStyle}
                          onFocus={commonFocusHandler as any}
                          onBlur={commonBlurHandler as any}
                          required={field.required}
                          disabled={awaitingSource}
                          value={valInSlots ? currentVal : ""}
                          onChange={(event) => {
                            const value = event.target.value;
                            setInlineForm((prev) =>
                              prev
                                ? {
                                    ...prev,
                                    values: { ...prev.values, [field.id]: value },
                                    errors: { ...prev.errors, [field.id]: "" },
                                  }
                                : prev,
                            );
                          }}
                        >
                          <option value="">
                            {awaitingSource ? `Select ${sourceLabel} first` : "Select a time..."}
                          </option>
                          {slots.map((slot) => (
                            <option key={`${key}_${slot.value}`} value={slot.value}>
                              {slot.label}
                            </option>
                          ))}
                        </select>
                        {fieldError ? <span className="text-xs text-rose-600">{fieldError}</span> : null}
                      </label>
                    );
                  }
                  return (
                    <label key={key} className="flex flex-col gap-1">
                      <span className="text-xs font-medium" style={{ color: widgetTextColor }}>
                        {field.label}
                        {field.required ? <span style={{ color: widgetUiColor }}> *</span> : ""}
                      </span>
                      <input
                        type="time"
                        className={commonClass}
                        style={commonStyle}
                        onFocus={commonFocusHandler}
                        onBlur={commonBlurHandler}
                        required={field.required}
                        value={inlineForm.values[field.id] || ""}
                        onChange={(event) => {
                          const value = event.target.value;
                          setInlineForm((prev) =>
                            prev
                              ? {
                                  ...prev,
                                  values: { ...prev.values, [field.id]: value },
                                  errors: { ...prev.errors, [field.id]: "" },
                                }
                              : prev,
                          );
                        }}
                      />
                      {fieldError ? <span className="text-xs text-rose-600">{fieldError}</span> : null}
                    </label>
                  );
                }

                if (field.type === "datetime-local" || field.type === "datetime") {
                  return (
                    <label key={key} className="flex flex-col gap-1">
                      <span className="text-xs font-medium" style={{ color: widgetTextColor }}>
                        {field.label}
                        {field.required ? <span style={{ color: widgetUiColor }}> *</span> : ""}
                      </span>
                      <input
                        type="datetime-local"
                        className={commonClass}
                        style={commonStyle}
                        onFocus={commonFocusHandler}
                        onBlur={commonBlurHandler}
                        required={field.required}
                        value={inlineForm.values[field.id] || ""}
                        onChange={(event) => {
                          const value = event.target.value;
                          setInlineForm((prev) =>
                            prev
                              ? {
                                  ...prev,
                                  values: { ...prev.values, [field.id]: value },
                                  errors: { ...prev.errors, [field.id]: "" },
                                }
                              : prev,
                          );
                        }}
                      />
                      {fieldError ? <span className="text-xs text-rose-600">{fieldError}</span> : null}
                    </label>
                  );
                }

                if ((field.type === "select" || field.type === "dropdown") && field.options.length > 0) {
                  return (
                    <label key={key} className="flex flex-col gap-1">
                      <span className="text-xs font-medium" style={{ color: widgetTextColor }}>
                        {field.label}
                        {field.required ? <span style={{ color: widgetUiColor }}> *</span> : ""}
                      </span>
                      <select
                        className={commonClass}
                        style={commonStyle}
                        onFocus={commonFocusHandler as any}
                        onBlur={commonBlurHandler as any}
                        required={field.required}
                        value={inlineForm.values[field.id] || ""}
                        onChange={(event) => {
                          const value = event.target.value;
                          setInlineForm((prev) => {
                            if (!prev) return prev;
                            const nextValues = { ...prev.values, [field.id]: value };
                            // Clear any time-slot field that filters by THIS field,
                            // so a stale time doesn't survive an outlet switch.
                            for (const f of prev.fields) {
                              if (f.slotSourceField && f.slotSourceField === field.id) {
                                nextValues[f.id] = "";
                              }
                            }
                            return {
                              ...prev,
                              values: nextValues,
                              errors: { ...prev.errors, [field.id]: "" },
                            };
                          });
                        }}
                      >
                        <option value="">Select...</option>
                        {field.options.map((option) => (
                          <option key={`${key}_${option}`} value={option}>
                            {option}
                          </option>
                        ))}
                      </select>
                      {fieldError ? <span className="text-xs text-rose-600">{fieldError}</span> : null}
                    </label>
                  );
                }

                return (
                  <label key={key} className="flex flex-col gap-1">
                    <span className="text-xs font-medium" style={{ color: widgetTextColor }}>
                      {field.label}
                      {field.required ? <span style={{ color: widgetUiColor }}> *</span> : ""}
                    </span>
                    <input
                      type={field.type === "number" ? "number" : field.type === "email" ? "email" : "text"}
                      className={commonClass}
                      style={commonStyle}
                      onFocus={commonFocusHandler}
                      onBlur={(event) => {
                        commonBlurHandler(event);
                        if (isNameField(field)) {
                          const nameErr =
                            validateNameInput(String(inlineForm.values[field.id] || "")) || "";
                          if (nameErr) {
                            setInlineForm((prev) =>
                              prev
                                ? { ...prev, errors: { ...prev.errors, [field.id]: nameErr } }
                                : prev,
                            );
                          }
                        }
                      }}
                      required={field.required}
                      placeholder={field.placeholder || undefined}
                      value={inlineForm.values[field.id] || ""}
                      onChange={(event) => {
                        const value = event.target.value;
                        setInlineForm((prev) =>
                          prev
                            ? {
                                ...prev,
                                values: { ...prev.values, [field.id]: value },
                                errors: { ...prev.errors, [field.id]: "" },
                              }
                            : prev,
                        );
                      }}
                    />
                    {fieldError ? <span className="text-xs text-rose-600">{fieldError}</span> : null}
                  </label>
                );
                    })}
                  </div>
                  <div className="mt-3 flex gap-2">
                    {isMobile && mobileFormStep > 0 ? (
                      <button
                        type="button"
                        onClick={() => setMobileFormStep((s) => s - 1)}
                        className="flex-1 rounded-lg border px-3 py-2 text-sm font-medium transition-opacity"
                        style={{ borderColor: `${widgetUiColor}40`, color: widgetUiColor }}
                      >
                        ← Back
                      </button>
                    ) : null}
                    {!isLastStep ? (
                      <button
                        type="button"
                        onClick={() => {
                          setMobileFormStepTransitioning(true);
                          setMobileFormStep((s) => s + 1);
                          setTimeout(() => setMobileFormStepTransitioning(false), 350);
                        }}
                        className="flex-1 rounded-lg px-3 py-2 text-sm font-medium text-white transition-opacity"
                        style={{ backgroundColor: widgetUiColor }}
                      >
                        Next →
                      </button>
                    ) : (
                      <button
                        type="submit"
                        disabled={inlineForm.submitting || inlineForm.cancelling || mobileFormStepTransitioning}
                        className="flex-1 rounded-lg px-3 py-2 text-sm font-medium text-white transition-opacity disabled:opacity-50"
                        style={{ backgroundColor: widgetUiColor }}
                      >
                        {inlineForm.submitting ? "Submitting..." : "Submit"}
                      </button>
                    )}
                    {inlineForm.cancellable ? (
                      <button
                        type="button"
                        onClick={handleInlineFormCancel}
                        disabled={inlineForm.submitting || inlineForm.cancelling}
                        className="rounded-lg border px-3 py-2 text-sm font-medium transition-opacity disabled:opacity-50"
                        style={{ borderColor: `${widgetUiColor}40`, color: widgetUiColor }}
                      >
                        {inlineForm.cancelling ? "Cancelling..." : "Cancel request"}
                      </button>
                    ) : null}
                  </div>
                </>
              );
            })()}
          </>
        )}
      </form>
    );
  };

  return (
    <div
      className={
        embedMode
          ? "h-screen w-full overflow-hidden font-sans"
          : "h-screen w-full overflow-y-auto bg-[radial-gradient(circle_at_top,_#f8fafc_0%,_#e2e8f0_55%,_#cbd5e1_100%)] text-slate-900 font-sans"
      }
      style={embedMode ? { backgroundColor: widgetBackgroundColor, color: widgetTextColor } : undefined}
    >
      {!embedMode && (
      <div className="mx-auto max-w-6xl p-4 sm:p-6">
        <div className="rounded-xl border border-slate-300/70 bg-white/85 p-4 shadow-sm backdrop-blur">
          <h1 className="text-lg font-semibold">Chat Widget Test Harness</h1>
          <p className="mt-1 text-sm text-slate-600">
            Configure hotel, phase, profile, booking, and metadata; then test through the same widget flow used in production.
          </p>

          <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <label className="flex flex-col gap-1">
              <span className="text-xs font-medium text-slate-600">Hotel</span>
              <select
                className="rounded border border-slate-300 bg-white px-2 py-1.5 text-sm"
                value={hotelCode}
                onChange={(event) => setHotelCode(event.target.value)}
              >
                {properties.map((row) => (
                  <option key={row.code} value={row.code}>
                    {[row.name, row.city].filter(Boolean).join(" - ") || row.code}
                  </option>
                ))}
              </select>
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-xs font-medium text-slate-600">Phase</span>
              <select
                className="rounded border border-slate-300 bg-white px-2 py-1.5 text-sm"
                value={phase}
                onChange={(event) => setPhase(normalizePhaseId(event.target.value) || "pre_booking")}
              >
                {phases.map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.name}
                  </option>
                ))}
              </select>
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-xs font-medium text-slate-600">Booking (phase scoped)</span>
              <select
                className="rounded border border-slate-300 bg-white px-2 py-1.5 text-sm"
                value={selectedBookingId === "" ? "" : String(selectedBookingId)}
                onChange={(event) => {
                  const value = event.target.value;
                  setSelectedBookingId(value ? Number(value) : "");
                }}
              >
                <option value="">{normalizePhaseId(phase) === "pre_booking" ? "N/A (pre-booking)" : "-- Select --"}</option>
                {bookings.map((row) => (
                  <option key={row.booking_id} value={row.booking_id}>
                    {`${row.guest_name || "Guest"} - ${row.room_number || "No room"} - ${row.property_name || ""}`}
                  </option>
                ))}
              </select>
            </label>

            <label className="flex flex-col gap-1">
              <span className="text-xs font-medium text-slate-600">
                LLM Model {multiModelEnabled ? "(multi-model)" : modelSaving ? "(saving…)" : ""}
              </span>
              <select
                className="rounded border border-slate-300 bg-white px-2 py-1.5 text-sm disabled:cursor-not-allowed disabled:bg-slate-100 disabled:text-slate-500"
                value={multiModelEnabled ? "__multi__" : activeModel}
                disabled={multiModelEnabled}
                onChange={(event) => {
                  if (multiModelEnabled) return;
                  void handleModelOverrideChange(event.target.value);
                }}
                title={
                  multiModelEnabled
                    ? `Disabled — multi-model is active, each role runs on its own model:\n${Object.entries(
                        roleModels,
                      )
                        .map(([role, id]) => `• ${role}: ${id}`)
                        .join("\n")}\n\nSet MULTI_MODEL_ENABLED=false and restart to pick a single model.`
                    : "Overrides the model for every LLM call in this deployment (test harness)."
                }
              >
                {multiModelEnabled ? (
                  <option value="__multi__">Multi-model (per role)</option>
                ) : (
                  <>
                    <option value="">
                      {defaultModel ? `Default (${defaultModel})` : "Default (env)"}
                    </option>
                    {modelOptions.map((row) => (
                      <option key={row.id} value={row.id}>
                        {row.label}
                      </option>
                    ))}
                  </>
                )}
              </select>
              {multiModelEnabled && Object.keys(roleModels).length > 0 ? (
                <span className="text-[10px] leading-tight text-slate-500">
                  routing {roleModels.main_router || "?"} · answer{" "}
                  {roleModels.book_answerer || "?"} · chips{" "}
                  {roleModels.next_suggestions || "?"}
                </span>
              ) : null}
            </label>

            <div className="flex items-end gap-2">
              <button
                type="button"
                className="rounded border border-slate-300 bg-white px-3 py-1.5 text-sm"
                onClick={() => setBookingModalOpen(true)}
              >
                Create Booking
              </button>
              <button
                type="button"
                className="rounded border border-slate-300 bg-white px-3 py-1.5 text-sm"
                onClick={() => {
                  resetLocalConversation(generateSessionId());
                }}
              >
                New Session
              </button>
            </div>
          </div>

          <div className="mt-3 grid gap-3 lg:grid-cols-2">
            <div className="rounded border border-slate-200 p-3">
              <div className="mb-2 flex items-center gap-2">
                <input
                  id="auto-phase-profile"
                  type="checkbox"
                  checked={autoPhaseProfile}
                  onChange={(event) => setAutoPhaseProfile(event.target.checked)}
                />
                <label htmlFor="auto-phase-profile" className="text-sm font-medium">
                  Auto-apply mapped test profile
                </label>
              </div>
              <p className="text-xs text-slate-600">{profileStatusText}</p>
              <div className="mt-2 grid gap-2">
                {(["guest_name", "guest_phone", "room_number", "guest_id", "entity_id", "organisation_id", "ticket_source", "flow"] as const).map(
                  (field) => (
                    <label key={field} className="flex flex-col gap-1">
                      <span className="text-[11px] uppercase tracking-wide text-slate-500">{field}</span>
                      <input
                        className="rounded border border-slate-300 bg-white px-2 py-1 text-sm"
                        value={profileOverrides[field] || ""}
                        placeholder={activePhaseProfile?.[field] || ""}
                        onChange={(event) =>
                          setProfileOverrides((prev) => ({
                            ...prev,
                            [field]: event.target.value,
                          }))
                        }
                      />
                    </label>
                  ),
                )}
              </div>
              <pre className="mt-2 max-h-28 overflow-auto rounded bg-slate-100 p-2 text-xs text-slate-700">
                {JSON.stringify(activePhaseProfile || {}, null, 2)}
              </pre>
            </div>
            <div className="rounded border border-slate-200 p-3">
              <p className="text-sm font-medium">Session & Ticket Snapshot</p>
              <div className="mt-2 grid gap-1 text-sm">
                <p>
                  <span className="font-medium">Session:</span> {sessionId}
                </p>
                <p>
                  <span className="font-medium">State:</span> {sessionState}
                </p>
                <p>
                  <span className="font-medium">Ticket:</span> {ticketStatus.label}
                </p>
                <p>
                  <span className="font-medium">Messages:</span> {messageCount}
                </p>
              </div>
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={handleResetSession}
                  className="rounded border border-slate-300 bg-white px-3 py-1.5 text-sm"
                >
                  Reset Session
                </button>
                <button
                  type="button"
                  onClick={handleLoadHistory}
                  className="rounded border border-slate-300 bg-white px-3 py-1.5 text-sm"
                >
                  View History
                </button>
                <button
                  type="button"
                  onClick={handleLoadTickets}
                  className="rounded border border-slate-300 bg-white px-3 py-1.5 text-sm"
                >
                  View Tickets
                </button>
              </div>
              <label className="mt-3 block">
                <span className="text-xs font-medium text-slate-600">Extra metadata JSON (optional)</span>
                <textarea
                  value={extraMetadataRaw}
                  onChange={(event) => setExtraMetadataRaw(event.target.value)}
                  className="mt-1 h-20 w-full rounded border border-slate-300 bg-white px-2 py-1.5 text-sm"
                  placeholder='{"user_type":"tester","channel_origin":"widget"}'
                />
              </label>
              <pre className="mt-2 max-h-28 overflow-auto rounded bg-slate-100 p-2 text-xs text-slate-700">{bookingDetailsText}</pre>
              {ticketDetails ? (
                <pre className="mt-2 max-h-28 overflow-auto rounded bg-emerald-50 p-2 text-xs text-emerald-900">
                  {JSON.stringify(ticketDetails, null, 2)}
                </pre>
              ) : null}
            </div>
          </div>

          {!embedMode && debugData ? (
            <details className="mt-3 rounded border border-slate-200 bg-slate-50 p-3">
              <summary className="cursor-pointer text-sm font-medium">Debug Payload</summary>
              <pre className="mt-2 max-h-64 overflow-auto text-xs text-slate-700">{JSON.stringify(debugData, null, 2)}</pre>
            </details>
          ) : null}
        </div>
      </div>
      )}

      <div className={embedMode ? "h-full w-full" : `fixed bottom-4 z-[9999] ${widgetDockClass}`}>
        <AnimatePresence>
          {!embedMode && !widgetOpen && (
            <motion.div
              initial={{ scale: 0, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              exit={{ scale: 0, opacity: 0 }}
              className={`flex flex-col gap-2 ${widgetPosition === "left" ? "items-start" : "items-end"}`}
            >
              {widgetShowTeaser && (
                <motion.div
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  className="relative max-w-[220px] rounded-xl border border-slate-200 bg-white p-3 shadow-lg"
                >
                  <button
                    type="button"
                    onClick={() => setWidgetShowTeaser(false)}
                    className="absolute -right-1.5 -top-1.5 flex h-5 w-5 items-center justify-center rounded-full border border-slate-200 bg-white text-[10px] text-slate-500 shadow-sm hover:bg-slate-100"
                  >
                    ×
                  </button>
                  <p className="text-xs font-medium text-slate-900">👋 Hi there!</p>
                  <p className="mt-0.5 text-[11px] text-slate-500">
                    Need help? Chat with {widgetBotName}
                  </p>
                </motion.div>
              )}

              <button
                type="button"
                onClick={() => {
                  setWidgetOpen(true);
                  setWidgetShowTeaser(false);
                }}
                className="flex h-14 w-14 items-center justify-center overflow-hidden rounded-full shadow-lg transition-transform hover:scale-105 active:scale-95"
                style={{
                  backgroundColor: widgetBrandColor,
                  boxShadow: `0 8px 24px -4px rgba(0,0,0,0.2), 0 0 24px -8px ${widgetBrandColor}80`,
                  border: "3px solid rgba(255,255,255,0.9)",
                }}
                aria-label="Open Chat"
              >
                {/* A configured logo is the launcher face too, so the closed bubble
                    and the open header show the same avatar. */}
                {widgetLogoUrl ? (
                  <img
                    src={widgetLogoUrl}
                    alt=""
                    aria-hidden="true"
                    className="h-full w-full rounded-full object-cover"
                    onError={() => setResolvedLogoUrl("")}
                  />
                ) : (
                  <MessageCircle className="h-6 w-6 text-white" />
                )}
              </button>
            </motion.div>
          )}
        </AnimatePresence>

        <AnimatePresence>
          {(embedMode || widgetOpen) && (
            <motion.div
              initial={{ opacity: 0, scale: 0.9, y: 20 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.9, y: 20 }}
              transition={{ type: "spring", damping: 25, stiffness: 300 }}
              className={
                embedMode
                  ? "flex h-full w-full flex-col overflow-hidden bg-white"
                  : "flex flex-col overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-2xl"
              }
              style={{
                width: embedMode
                  ? "100%"
                  : isMobile
                    ? "100vw"
                    : Math.min(widgetExpanded ? WIDGET_EXPANDED_WIDTH : widgetDefaultWidth, 600),
                height: embedMode
                  ? "100%"
                  : widgetMinimized
                    ? 56
                    : isMobile
                      ? "100dvh"
                      : Math.min(
                          widgetExpanded ? WIDGET_EXPANDED_HEIGHT : widgetDefaultHeight,
                          typeof window !== "undefined" ? window.innerHeight - 48 : 700,
                        ),
                position: embedMode ? "relative" : isMobile ? "fixed" : "relative",
                top: embedMode ? undefined : isMobile ? 0 : undefined,
                left: embedMode ? undefined : isMobile ? 0 : undefined,
                right: embedMode ? undefined : isMobile ? 0 : undefined,
                bottom: embedMode ? undefined : isMobile ? 0 : undefined,
                borderRadius: embedMode ? 0 : isMobile ? 0 : undefined,
              }}
            >
              <div
                className="sticky top-0 z-[2] flex shrink-0 items-center justify-between px-4 py-3"
                style={{ backgroundColor: widgetBrandColor }}
              >
                <div className="flex items-center gap-2">
                  {widgetLogoUrl ? (
                    <img
                      src={widgetLogoUrl}
                      alt={widgetBotName}
                      className="h-8 w-8 rounded-full object-cover border border-white/30"
                      // A logo that 404s (wrong host, asset not deployed) used to leave a
                      // blank gap; drop back to the bot-name initial instead.
                      onError={() => setResolvedLogoUrl("")}
                    />
                  ) : (
                    <div className="flex h-8 w-8 items-center justify-center rounded-full bg-white/20 text-sm font-bold text-white">
                      {widgetBotName[0] || "A"}
                    </div>
                  )}
                  <div>
                    <h3 className="text-sm font-semibold text-white">{widgetBotName}</h3>
                    <div className="flex items-center gap-1">
                      <span
                        className={`h-1.5 w-1.5 rounded-full ${
                          botOnline
                            ? "animate-pulse bg-green-400"
                            : wsStatus === "connecting"
                              ? "animate-pulse bg-yellow-400"
                              : wsStatus === "open"
                                ? "bg-amber-500"
                                : "bg-slate-400"
                        }`}
                        title={
                          botOnline
                            ? "WebSocket connected"
                            : wsStatus === "open"
                              ? "Connected, but the assistant cannot answer right now"
                              : wsStatus === "http-fallback"
                                ? "HTTP mode"
                                : wsStatus
                        }
                      />
                      <span className="text-[10px] text-white/80">
                        {embedMode ? botStatusLabel : `${hotelCode} • ${normalizePhaseId(phase) || phase}`}
                      </span>
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-0.5">
                  {!widgetMinimized && (
                    <button
                      type="button"
                      onClick={() => startNewChat()}
                      className="rounded-lg p-2 text-white/80 transition-colors hover:bg-white/20 hover:text-white"
                      title="Clear chat"
                    >
                      <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6" strokeLinecap="round" strokeLinejoin="round"/>
                      </svg>
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => setWidgetMinimized((v) => !v)}
                    className="rounded-lg p-2 text-white/80 transition-colors hover:bg-white/20 hover:text-white"
                    title={widgetMinimized ? "Restore" : "Minimize"}
                  >
                    {widgetMinimized ? <Plus className="h-4 w-4" /> : <Minus className="h-4 w-4" />}
                  </button>
                  {!widgetMinimized && (!isMobile || embedMode) && (
                    <button
                      type="button"
                      onClick={() => setWidgetExpanded((v) => !v)}
                      className="rounded-lg p-2 text-white/80 transition-colors hover:bg-white/20 hover:text-white"
                      title={widgetExpanded ? "Shrink" : "Expand"}
                    >
                      <Maximize2 className="h-4 w-4" />
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => {
                      if (embedMode) {
                        postEmbedEvent("widget:close");
                        return;
                      }
                      setWidgetOpen(false);
                    }}
                    className="rounded-lg p-2 text-white/80 transition-colors hover:bg-white/20 hover:text-white"
                    title={embedMode ? "Hide" : "Close"}
                  >
                    <X className="h-4 w-4" />
                  </button>
                </div>
              </div>

              {!widgetMinimized && (
                <div className="flex min-h-0 flex-1 flex-col">
            {/* Agent handoff status banner */}
            {agentHandoverStatus !== "bot" && (
              <div
                className="shrink-0 flex items-center gap-2 px-3 py-1.5 text-xs"
                style={{
                  backgroundColor: agentHandoverStatus === "waiting" ? "#fefce8" : "#eff6ff",
                  borderBottom: `1px solid ${agentHandoverStatus === "waiting" ? "#fde047" : "#bfdbfe"}`,
                  color: agentHandoverStatus === "waiting" ? "#854d0e" : "#1d4ed8",
                }}
              >
                {agentHandoverStatus === "waiting" ? (
                  <>
                    <span className="h-2 w-2 rounded-full bg-yellow-400 animate-pulse shrink-0" />
                    <span>Connecting you to a live agent...</span>
                  </>
                ) : (
                  <>
                    <User className="h-3.5 w-3.5 shrink-0" />
                    <span>
                      <strong>{agentName || "An agent"}</strong> is assisting you
                    </span>
                  </>
                )}
              </div>
            )}
            <div
              ref={messagesScrollRef}
              className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden p-3"
              style={{ backgroundColor: widgetBackgroundColor, color: widgetTextColor }}
            >
              {messages.length === 0 ? (
                <div className="rounded border border-dashed border-slate-300 bg-white p-3 text-sm" style={{ color: widgetTextColor }}>
                  {embedMode ? "Start a conversation." : "Start a conversation to test backend behavior in widget mode."}
                </div>
              ) : null}

              <div className="space-y-2">
                {messages.map((row) => {
                  // ── Opening greeting bubble: hidden when the intro form is
                  // the opener (multi-property pre-booking). The form card
                  // already greets the guest ("Hey there! Let's get you set
                  // up"), so rendering the bubble too showed two greetings for
                  // one action. The row stays in `messages` — only its render
                  // is skipped — so the restore / conversation-rotation logic
                  // that keys off `welcome_*` is untouched.
                  if (suppressWelcomeBubble && String(row.id || "").startsWith("welcome_")) {
                    return null;
                  }
                  // ── System message: centered status banner ────────────
                  if (row.role === "system") {
                    return (
                      <div key={row.id} className="flex justify-center my-1">
                        <div
                          className="text-[11px] rounded-full px-3 py-1 text-center"
                          role="status"
                          style={{ backgroundColor: `${widgetUiColor}15`, color: `${widgetUiColor}cc` }}
                        >
                          {row.content}
                        </div>
                      </div>
                    );
                  }

                  // ── Agent message: left-aligned with person icon ──────
                  if (row.role === "agent") {
                    return (
                      <div
                        key={row.id}
                        ref={(node) => {
                          if (node) messageRowRefs.current[row.id] = node;
                          else delete messageRowRefs.current[row.id];
                        }}
                        className="flex justify-start items-end gap-2"
                      >
                        <div
                          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full"
                          style={{ backgroundColor: "#dbeafe" }}
                        >
                          <User className="h-3.5 w-3.5" style={{ color: "#1d4ed8" }} />
                        </div>
                        <div
                          className="max-w-[82%] rounded-2xl rounded-bl-sm px-3.5 py-2 text-sm whitespace-pre-wrap shadow-sm"
                          style={{ backgroundColor: "#eff6ff", border: "1px solid #bfdbfe", color: "#1e3a5f" }}
                        >
                          <p className="leading-relaxed">{row.content}</p>
                          <p className="mt-1 text-[10px] opacity-60 text-right">
                            {row.timestamp ? new Date(row.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : ""}
                          </p>
                        </div>
                      </div>
                    );
                  }

                  // ── Bot / user messages ───────────────────────────────
                  const isAssistant = row.role === "assistant";
                  const isInlineTarget = inlineForm && inlineForm.messageId === row.id;
                  const imageCards = isAssistant
                    ? normalizeChatImages((row.raw?.metadata || {}).images)
                    : [];
                  const documentCards = isAssistant
                    ? normalizeChatDocuments((row.raw?.metadata || {}).documents)
                    : [];
                  return (
                    <div
                      key={row.id}
                      ref={(node) => {
                        if (node) {
                          messageRowRefs.current[row.id] = node;
                        } else {
                          delete messageRowRefs.current[row.id];
                        }
                      }}
                      className={isAssistant ? "flex justify-start" : "flex justify-end"}
                    >
                      <div
                        className={`max-w-[86%] rounded-2xl px-3.5 py-2 text-sm whitespace-pre-wrap shadow-sm ${
                          isAssistant
                            ? "rounded-bl-sm border border-slate-200 bg-white text-slate-900"
                            : "rounded-br-sm text-white"
                        }`}
                        style={isAssistant ? { color: widgetTextColor } : { backgroundColor: widgetUiColor, color: "#ffffff" }}
                      >
                        {isAssistant ? (
                          (() => {
                            const isLong = (row.content?.length || 0) > LONG_ASSISTANT_MESSAGE_CHARS;
                            const isExpanded = expandedMsgs.has(row.id);
                            const collapsed = isLong && !isExpanded;
                            return (
                              <>
                                <div className={collapsed ? "relative max-h-[210px] overflow-hidden" : undefined}>
                                  <p
                                    className="leading-relaxed"
                                    dangerouslySetInnerHTML={{ __html: renderAssistantMarkdownToHtml(row.content) }}
                                  />
                                  {collapsed ? (
                                    <div
                                      className="pointer-events-none absolute inset-x-0 bottom-0 h-12 bg-gradient-to-t from-white to-transparent"
                                    />
                                  ) : null}
                                </div>
                                {isLong ? (
                                  <button
                                    type="button"
                                    onClick={() => toggleMsgExpanded(row.id)}
                                    className="mt-1 text-xs font-semibold hover:underline"
                                    style={{ color: widgetUiColor }}
                                  >
                                    {isExpanded ? "Show less" : "Read more"}
                                  </button>
                                ) : null}
                              </>
                            );
                          })()
                        ) : (
                          <p>{row.content}</p>
                        )}
                        {isAssistant && imageCards.length > 0 ? (
                          <AssistantImageCarousel images={imageCards} accentColor={widgetUiColor} />
                        ) : null}
                        {isAssistant && documentCards.length > 0 ? (
                          <AssistantDocumentCards
                            documents={documentCards}
                            apiBase={runtimeConfig.apiUrl}
                            accentColor={widgetUiColor}
                          />
                        ) : null}
                        {isAssistant && (row.raw?.metadata as Record<string, unknown> | undefined)?.booking_cta ? (
                          (() => {
                            const rawCta = (row.raw?.metadata as Record<string, unknown>).booking_cta;
                            const ctaList = (Array.isArray(rawCta) ? rawCta : [rawCta]) as Array<{
                              url?: string;
                              label?: string;
                            }>;
                            const buttons = ctaList
                              .map((c) => ({ href: normalizeSafeLink(String(c?.url || "")), label: String(c?.label || "Book on website") }))
                              .filter((b) => b.href);
                            if (buttons.length === 0) return null;
                            return (
                              <div className="mt-2 flex flex-col gap-2">
                                {buttons.map((b, i) => (
                                  <a
                                    key={`${b.href}_${i}`}
                                    href={b.href}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    onClick={() => setBookingLinkClicked(true)}
                                    className="inline-flex items-center justify-center rounded-full px-4 py-2 text-sm font-medium text-white shadow-sm transition-opacity hover:opacity-90"
                                    style={{ backgroundColor: widgetUiColor }}
                                  >
                                    {b.label}
                                  </a>
                                ))}
                              </div>
                            );
                          })()
                        ) : null}
                        {/* Link buttons an admin set on the service that answered
                            (Bot Training -> Pre-booking -> Services). The URL is
                            already resolved for the hotel in focus. */}
                        {isAssistant
                          ? (() => {
                              const raw = ((row.raw?.metadata || {}) as Record<string, unknown>).service_links;
                              const links = (Array.isArray(raw) ? raw : [])
                                .map((c) => ({
                                  href: normalizeSafeLink(String((c as Record<string, unknown>)?.url || "")),
                                  label: String((c as Record<string, unknown>)?.label || "").trim(),
                                }))
                                .filter((b) => b.href && b.label);
                              if (links.length === 0) return null;
                              return (
                                <div className="mt-2 flex flex-col gap-2">
                                  {links.map((b, i) => (
                                    <a
                                      key={`svc_link_${b.href}_${i}`}
                                      href={b.href}
                                      target="_blank"
                                      rel="noopener noreferrer"
                                      className="inline-flex items-center justify-center rounded-full px-4 py-2 text-sm font-medium text-white shadow-sm transition-opacity hover:opacity-90"
                                      style={{ backgroundColor: widgetUiColor }}
                                    >
                                      {b.label}
                                    </a>
                                  ))}
                                </div>
                              );
                            })()
                          : null}
                        {/* "Get help from our team" — sits under the website link
                            on room-booking turns. Opens a small form; submitting
                            it enriches the room-booking ticket that was already
                            raised for this stay rather than creating a new one. */}
                        {isAssistant
                          ? (() => {
                              const assist = ((row.raw?.metadata || {}) as Record<string, unknown>)
                                .assist_cta as
                                | { label?: string; property_name?: string; once_per_conversation?: boolean }
                                | undefined;
                              if (!assist) return null;
                              // Team-only booking flow: one submission per
                              // conversation, then the button is done for good.
                              if (assist.once_per_conversation && assistSubmittedThisConvo) return null;
                              if (assistForm?.messageId === row.id) {
                                return renderAssistForm();
                              }
                              if (assistDoneIds.has(row.id)) return null;
                              return (
                                <div className="mt-2 flex flex-col gap-2">
                                  <button
                                    type="button"
                                    onClick={() =>
                                      setAssistForm({
                                        messageId: row.id,
                                        propertyName: String(assist.property_name || "").trim(),
                                        checkIn: "",
                                        checkOut: "",
                                        guests: "",
                                        message: "",
                                        errors: {},
                                        submitting: false,
                                      })
                                    }
                                    className="inline-flex items-center justify-center rounded-full border px-4 py-2 text-sm font-medium shadow-sm transition-colors hover:bg-slate-50"
                                    style={{ borderColor: `${widgetUiColor}66`, color: widgetUiColor }}
                                  >
                                    {String(assist.label || "Get help from our team")}
                                  </button>
                                </div>
                              );
                            })()
                          : null}
                        {/* "Share your event details" — the events twin of the
                            room-booking button above. The banquet agent used to
                            end its answer by ASKING whether to submit an enquiry,
                            which cost the guest a "yes" and a whole extra turn to
                            reach a form we already had. The fields ride along on
                            the CTA, so tapping opens the very same enquiry form
                            that answering "yes" would have produced. */}
                        {isAssistant
                          ? (() => {
                              const evt = ((row.raw?.metadata || {}) as Record<string, unknown>)
                                .event_cta as
                                | { label?: string; service_id?: string; fields?: unknown; once_per_conversation?: boolean }
                                | undefined;
                              if (!evt) return null;
                              if (evt.once_per_conversation && eventSubmittedThisConvo) return null;
                              // The form itself renders through the normal inline
                              // form path, so don't offer the button while it is open.
                              if (inlineForm?.messageId === row.id) return null;
                              const evtFields = normalizeFormFields(
                                Array.isArray(evt.fields) ? evt.fields : [],
                              );
                              if (evtFields.length === 0) return null;
                              return (
                                <div className="mt-2 flex flex-col gap-2">
                                  <button
                                    type="button"
                                    onClick={() => {
                                      const values: Record<string, string> = {};
                                      evtFields.forEach((f) => {
                                        values[f.id] = String(f.default_value || "");
                                      });
                                      setInlineForm({
                                        messageId: row.id,
                                        serviceId: String(evt.service_id || "banquet_event_enquiry"),
                                        fields: evtFields,
                                        values,
                                        countryCodes: {},
                                        errors: {},
                                        submitting: false,
                                        successMessage: "",
                                        cancellable: true,
                                        cancelling: false,
                                      });
                                      setSuggestions([]);
                                    }}
                                    className="inline-flex items-center justify-center rounded-full border px-4 py-2 text-sm font-medium shadow-sm transition-colors hover:bg-slate-50"
                                    style={{ borderColor: `${widgetUiColor}66`, color: widgetUiColor }}
                                  >
                                    {String(evt.label || "Share your event details")}
                                  </button>
                                </div>
                              );
                            })()
                          : null}
                        {/* Per-hotel service CTAs: offered under the opening bot
                            message so a new guest can jump straight to the lane
                            they want (rooms / events) instead of having to phrase
                            it. Shown only while the conversation is still at its
                            start, and only for hotels configured in
                            chat_welcome_service_ctas_json — so no other tenant's
                            widget changes. Tapping sends the label, which the
                            router hands to the matching service agent. */}
                        {/* (The opening service CTAs live on the welcome bubble
                            beside "Book a Stay", not under each bot reply.) */}
                        {/* Multi-property city intro: one button per hotel so the
                            guest taps a name instead of typing it. Backend sends
                            these ONLY on the curated location-intro turn (and only
                            when 2+ properties are in that city), via a dedicated
                            metadata key so they can't leak onto other turns. */}
                        {isAssistant
                          ? (() => {
                              const choices = ((row.raw?.metadata || {}) as Record<string, unknown>)
                                .property_choices;
                              const list = Array.isArray(choices)
                                ? choices.map((c) => String(c || "").trim()).filter(Boolean)
                                : [];
                              if (list.length === 0) return null;
                              return (
                                <div className="mt-2 flex flex-col gap-2">
                                  {list.map((label, i) => (
                                    <button
                                      key={`choice_${label}_${i}`}
                                      type="button"
                                      onClick={() => void sendMessage(label, "property_choice")}
                                      className="inline-flex items-center justify-center rounded-full px-4 py-2 text-sm font-medium text-white shadow-sm transition-opacity hover:opacity-90"
                                      style={{ backgroundColor: widgetUiColor }}
                                    >
                                      {label}
                                    </button>
                                  ))}
                                </div>
                              );
                            })()
                          : null}
                        {isAssistant && Array.isArray(row.suggested_actions) && row.suggested_actions.length > 0 ? (
                          <div className="mt-2 flex flex-col gap-2">
                            {row.suggested_actions.filter(Boolean).map((label, i) => (
                              <button
                                key={`${label}_${i}`}
                                type="button"
                                onClick={() => void sendMessage(String(label), "welcome_cta")}
                                className="inline-flex items-center justify-center rounded-full px-4 py-2 text-sm font-medium text-white shadow-sm transition-opacity hover:opacity-90"
                                style={{ backgroundColor: widgetUiColor }}
                              >
                                {label}
                              </button>
                            ))}
                          </div>
                        ) : null}
                        {isAssistant && row.kind === "location_picker" ? (
                          <select
                            value={chosenLocationLabel}
                            disabled={locationChosen}
                            onChange={(event) => {
                              const entry = locationManifest.find((m) => m.label === event.target.value);
                              if (entry) handleLocationPickerSelect(entry);
                            }}
                            className="mt-2 w-full rounded border px-2 py-1.5 text-sm disabled:opacity-70"
                            style={{ borderColor: `${widgetUiColor}55`, color: widgetTextColor }}
                          >
                            <option value="">Choose the city you want to stay in…</option>
                            {locationManifest.map((entry) => (
                              <option key={entry.label} value={entry.label}>{entry.label}</option>
                            ))}
                          </select>
                        ) : null}
                        {isInlineTarget ? renderInlineForm() : null}
                        {/* Timestamp */}
                        <p className="mt-1 text-[10px] opacity-60 text-right">
                          {row.timestamp ? new Date(row.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : ""}
                        </p>
                        {/* ── Delivery ticks (user messages only) ───────── */}
                        {!isAssistant && row.status ? (
                          <div className="mt-1 flex justify-end items-center gap-0.5">
                            {row.status === "sending" && (
                              <svg className="h-3 w-3 opacity-50" viewBox="0 0 16 16" fill="currentColor">
                                <path d="M13.5 3.5L6 11 2.5 7.5l-1 1L6 13l8.5-8.5z" />
                              </svg>
                            )}
                            {row.status === "sent" && (
                              <svg className="h-3 w-3 opacity-70" viewBox="0 0 16 16" fill="currentColor">
                                <path d="M13.5 3.5L6 11 2.5 7.5l-1 1L6 13l8.5-8.5z" />
                              </svg>
                            )}
                            {row.status === "delivered" && (
                              <>
                                <svg className="h-3 w-3" viewBox="0 0 16 16" fill="currentColor" style={{ color: "#a8d8a8" }}>
                                  <path d="M13.5 3.5L6 11 2.5 7.5l-1 1L6 13l8.5-8.5z" />
                                </svg>
                                <svg className="h-3 w-3 -ml-1.5" viewBox="0 0 16 16" fill="currentColor" style={{ color: "#a8d8a8" }}>
                                  <path d="M13.5 3.5L6 11 2.5 7.5l-1 1L6 13l8.5-8.5z" />
                                </svg>
                              </>
                            )}
                            {row.status === "error" && (
                              <svg className="h-3 w-3 text-red-300" viewBox="0 0 16 16" fill="currentColor">
                                <path d="M8 1a7 7 0 100 14A7 7 0 008 1zm.75 4.5v4h-1.5v-4h1.5zm0 5.5v1.5h-1.5V11h1.5z" />
                              </svg>
                            )}
                          </div>
                        ) : null}
                      </div>
                    </div>
                  );
                })}
                {/* Bot thinking indicator — hidden when agent is active */}
                {(isSending || isTyping) && !isAgent ? (
                  <div className="flex justify-start">
                    <div className="flex items-center gap-1.5 rounded-2xl rounded-bl-sm border border-slate-200 bg-white px-3.5 py-2.5 shadow-sm">
                      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-slate-400 [animation-delay:-0.3s]" />
                      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-slate-400 [animation-delay:-0.15s]" />
                      <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-slate-400" />
                    </div>
                  </div>
                ) : null}
                {/* Agent typing indicator */}
                {isAgentTyping && isAgent ? (
                  <div className="flex justify-start items-end gap-2">
                    <div
                      className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full"
                      style={{ backgroundColor: "#dbeafe" }}
                    >
                      <User className="h-3.5 w-3.5" style={{ color: "#1d4ed8" }} />
                    </div>
                    <div
                      className="flex items-center gap-1.5 rounded-2xl rounded-bl-sm px-3.5 py-2.5 shadow-sm"
                      style={{ backgroundColor: "#eff6ff", border: "1px solid #bfdbfe" }}
                    >
                      <span className="h-1.5 w-1.5 animate-bounce rounded-full [animation-delay:-0.3s]" style={{ backgroundColor: "#1d4ed8" }} />
                      <span className="h-1.5 w-1.5 animate-bounce rounded-full [animation-delay:-0.15s]" style={{ backgroundColor: "#1d4ed8" }} />
                      <span className="h-1.5 w-1.5 animate-bounce rounded-full" style={{ backgroundColor: "#1d4ed8" }} />
                    </div>
                  </div>
                ) : null}
                {welcomeCtaEligible ? (
                  <div className="flex justify-start">
                    <div className="max-w-[86%] space-y-2">
                      {!introFormRequired && !locationPromptActive ? (
                        prebookingConfig ? (
                          // Opening buttons set in Bot Training -> Pre-booking.
                          prebookingConfig.buttons.length > 0 ? (
                            <div className="flex flex-wrap gap-2">
                              {prebookingConfig.buttons.map((button, i) => (
                                <button
                                  key={`welcome_btn_${i}_${button.label}`}
                                  type="button"
                                  onClick={() => handleAdminWelcomeButton(button)}
                                  className="rounded-full px-4 py-2 text-sm font-medium text-white shadow-sm transition-opacity hover:opacity-90"
                                  style={{ backgroundColor: widgetUiColor }}
                                >
                                  {button.label}
                                </button>
                              ))}
                            </div>
                          ) : null
                        ) : (
                          <div className="flex flex-wrap gap-2">
                            <button
                              type="button"
                              onClick={() => handleWelcomeCtaClick("stay")}
                              className="rounded-full px-4 py-2 text-sm font-medium text-white shadow-sm transition-opacity hover:opacity-90"
                              style={{ backgroundColor: widgetUiColor }}
                            >
                              Book a Stay
                            </button>
                            {/* Second opening CTA, only for hotels that sell events
                                (configured in chat_welcome_service_ctas_json). A hotel
                                without that config shows just "Book a Stay", exactly
                                as before. */}
                            {welcomeServiceCtas.length > 0 ? (
                              <button
                                type="button"
                                onClick={() => handleWelcomeCtaClick("venue")}
                                className="rounded-full px-4 py-2 text-sm font-medium text-white shadow-sm transition-opacity hover:opacity-90"
                                style={{ backgroundColor: widgetUiColor }}
                              >
                                {String(welcomeServiceCtas[0] || "Book a Venue")}
                              </button>
                            ) : null}
                          </div>
                        )
                      ) : null}
                      {ctaContactOpen ? (
                        (() => {
                          const ctaPhoneHelper = formatPhonePreview(ctaContactValues.phone, ctaContactPhoneCode);
                          const fieldBase =
                            "w-full rounded-lg border bg-white px-3 py-2 text-sm outline-none transition focus:ring-2";
                          const labelCls = "mb-1 block text-[11px] font-semibold uppercase tracking-wide text-slate-500";
                          // One configured field -> its input. The inputs
                          // themselves are unchanged from when this form was
                          // hardcoded: the phone still gets the country picker
                          // and digit clamping, the city still reads the live
                          // property manifest. Only which fields appear, their
                          // order, labels and placeholders now come from config.
                          const errStyle = (key: string) => ({
                            borderColor: ctaContactErrors[key] ? "#dc2626" : `${widgetUiColor}40`,
                            color: widgetTextColor,
                            ["--tw-ring-color" as string]: `${widgetUiColor}55`,
                          });
                          const fieldError = (key: string) =>
                            ctaContactErrors[key] ? (
                              <p className="mt-1 text-[11px] text-red-600">{ctaContactErrors[key]}</p>
                            ) : null;

                          const renderLeadField = (field: LeadFormField, index: number) => {
                            const key = `${field.fieldType}-${index}`;
                            switch (field.fieldType) {
                              case "MOBILE_NUMBER":
                              case "ALTERNATE_MOBILE_NUMBER":
                                return (
                                  <div key={key}>
                                    <label className={labelCls}>{field.label}</label>
                                    <div className="flex gap-2">
                                      <PhoneCountryPicker
                                        value={ctaContactPhoneCode}
                                        onChange={(country) => {
                                          // The new country may allow fewer digits.
                                          setCtaContactPhoneCode(country);
                                          setCtaContactValues((p) => ({
                                            ...p,
                                            phone: clampPhoneDigits(p.phone, country),
                                          }));
                                        }}
                                        accentColor={widgetUiColor}
                                        className="flex h-full items-center gap-1 whitespace-nowrap rounded-lg border bg-white px-2 py-2 text-sm outline-none focus:ring-2"
                                        style={{ borderColor: `${widgetUiColor}40`, color: widgetTextColor, ["--tw-ring-color" as string]: `${widgetUiColor}55` }}
                                      />
                                      <input
                                        type="tel"
                                        inputMode="numeric"
                                        maxLength={PHONE_MAX_DIGITS}
                                        value={ctaContactValues.phone}
                                        onChange={(e) =>
                                          setCtaContactValues((p) => ({
                                            ...p,
                                            phone: clampPhoneDigits(e.target.value, ctaContactPhoneCode),
                                          }))
                                        }
                                        placeholder={field.placeholder || "Phone number"}
                                        className={fieldBase}
                                        style={errStyle("phone")}
                                      />
                                    </div>
                                    <div className="mt-1 flex items-center justify-between">
                                      <span className="text-[11px] text-red-600">{ctaContactErrors.phone || ""}</span>
                                      <span className="text-[11px] text-slate-400">{ctaPhoneHelper}</span>
                                    </div>
                                  </div>
                                );

                              case "CITY":
                                // A single-property hotel has no city to choose,
                                // so this field stays hidden there exactly as before;
                                // so does a hotel whose properties share one location.
                                if (!ctaIsMultiProperty || singleLocationLabel) return null;
                                return (
                                  <div key={key}>
                                    <label className={labelCls}>{field.label}</label>
                                    <select
                                      value={ctaContactValues.location}
                                      onChange={(e) => setCtaContactValues((p) => ({ ...p, location: e.target.value }))}
                                      className={fieldBase}
                                      style={errStyle("location")}
                                    >
                                      <option value="">{field.placeholder || "Choose the city you want to stay in…"}</option>
                                      {locationManifest.map((entry) => (
                                        <option key={entry.label} value={entry.label}>{entry.label}</option>
                                      ))}
                                    </select>
                                    {fieldError("location")}
                                  </div>
                                );

                              case "EMAIL":
                                return (
                                  <div key={key}>
                                    <label className={labelCls}>{field.label}</label>
                                    <input
                                      type="email"
                                      value={ctaContactValues.email}
                                      onChange={(e) => setCtaContactValues((p) => ({ ...p, email: e.target.value }))}
                                      placeholder={field.placeholder || "you@example.com"}
                                      className={fieldBase}
                                      style={errStyle("email")}
                                    />
                                    {fieldError("email")}
                                  </div>
                                );

                              case "FULL_NAME":
                                return (
                                  <div key={key}>
                                    <label className={labelCls}>{field.label}</label>
                                    <input
                                      type="text"
                                      value={ctaContactValues.name}
                                      onChange={(e) => setCtaContactValues((p) => ({ ...p, name: e.target.value }))}
                                      placeholder={field.placeholder || "Your full name"}
                                      className={fieldBase}
                                      style={errStyle("name")}
                                    />
                                    {fieldError("name")}
                                  </div>
                                );

                              default: {
                                // An operator-invented field. It has no dedicated
                                // slot on the guest record, so it rides along in
                                // `extras` and is submitted with the rest.
                                const slot = `custom:${field.label}`;
                                return (
                                  <div key={key}>
                                    <label className={labelCls}>{field.label}</label>
                                    <input
                                      type={field.type === "number" ? "number" : "text"}
                                      value={ctaContactValues.extras?.[slot] ?? ""}
                                      onChange={(e) =>
                                        setCtaContactValues((p) => ({
                                          ...p,
                                          extras: { ...(p.extras || {}), [slot]: e.target.value },
                                        }))
                                      }
                                      placeholder={field.placeholder || ""}
                                      className={fieldBase}
                                      style={errStyle(slot)}
                                    />
                                    {fieldError(slot)}
                                  </div>
                                );
                              }
                            }
                          };
                          return (
                            <form
                              onSubmit={handleCtaContactSubmit}
                              className="space-y-3 rounded-2xl border bg-white p-4 shadow-lg"
                              style={{ borderColor: `${widgetUiColor}26` }}
                            >
                              <div>
                                <div className="text-sm font-semibold" style={{ color: widgetUiColor }}>
                                  {leadFormCopy.title}
                                </div>
                                <div className="mt-0.5 text-[11px] text-slate-500">
                                  {leadFormCopy.subtitle}
                                </div>
                              </div>
                              {leadFormFields.map(renderLeadField)}
                              {leadFormCopy.consent ? (
                                <p className="text-[11px] leading-snug text-slate-500">{leadFormCopy.consent}</p>
                              ) : null}
                              <button
                                type="submit"
                                className="w-full rounded-xl px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition-opacity hover:opacity-90"
                                style={{ backgroundColor: widgetUiColor }}
                              >
                                {leadFormSubmitLabel}
                              </button>
                            </form>
                          );
                        })()
                      ) : null}
                      {!ctaContactOpen && ctaIsMultiProperty && (locationPickerOpen || inputGated) ? (
                        locationPickerOpen ? (
                          <div
                            className="rounded-lg border bg-white shadow-md"
                            style={{ borderColor: `${widgetUiColor}40` }}
                          >
                            <div
                              className="px-3 pt-2 pb-1 text-xs font-medium"
                              style={{ color: widgetUiColor }}
                            >
                              Which city are you planning your stay in?
                            </div>
                            {locationManifest.length >= 8 ? (
                              <div className="px-3 pb-2">
                                <input
                                  type="text"
                                  value={locationSearch}
                                  onChange={(event) => setLocationSearch(event.target.value)}
                                  placeholder="Search cities..."
                                  className="w-full rounded border px-2 py-1 text-xs"
                                  style={{ borderColor: `${widgetUiColor}40`, color: widgetTextColor }}
                                />
                              </div>
                            ) : null}
                            <div className="max-h-60 overflow-auto">
                              {filteredLocationOptions.length === 0 ? (
                                <div className="px-3 py-2 text-xs text-slate-500">
                                  No matches.
                                </div>
                              ) : (
                                filteredLocationOptions.map((entry) => (
                                  <button
                                    key={entry.label}
                                    type="button"
                                    onClick={() => handleLocationPickerSelect(entry)}
                                    className="block w-full px-3 py-2 text-left text-xs transition-colors hover:bg-slate-50"
                                    style={{ color: widgetTextColor }}
                                  >
                                    {entry.label}
                                  </button>
                                ))
                              )}
                            </div>
                          </div>
                        ) : (
                          <button
                            type="button"
                            onClick={() => setLocationPickerOpen(true)}
                            className="w-full rounded-xl px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition-opacity hover:opacity-90"
                            style={{ backgroundColor: widgetUiColor }}
                          >
                            Choose location
                          </button>
                        )
                      ) : null}
                    </div>
                  </div>
                ) : null}
              </div>

              <div ref={messageEndRef} />
            </div>

            {/* Suggestion chips: rendered just above the input box (not inside
                the scrollable messages container) so they stay visible and
                anchored to the input as the conversation grows. */}
            {inlineForm && !inlineForm.successMessage ? null : suggestions.length > 0 ? (
              <div
                className={`shrink-0 border-t pt-2 pb-1 ${isMobile ? "chip-scroll-row flex gap-2 overflow-x-auto px-3" : "flex flex-wrap gap-2 px-3"}`}
                style={{
                  backgroundColor: widgetBackgroundColor,
                  borderTopColor: `${widgetUiColor}33`,
                  ...(isMobile ? { scrollbarWidth: "none" } : {}),
                }}
              >
                {suggestions.map((item, idx) => (
                  <button
                    key={`${item}_${idx}`}
                    type="button"
                    onClick={() => void sendMessage(item, "suggested_action")}
                    className={`rounded-full border bg-white px-3 py-1 text-xs transition-colors ${isMobile ? "shrink-0" : ""}`}
                    style={{
                      borderColor: `${widgetUiColor}40`,
                      color: widgetUiColor,
                    }}
                    onMouseEnter={(e) => {
                      (e.currentTarget as HTMLButtonElement).style.backgroundColor = widgetUiColor;
                      (e.currentTarget as HTMLButtonElement).style.color = "white";
                    }}
                    onMouseLeave={(e) => {
                      (e.currentTarget as HTMLButtonElement).style.backgroundColor = "white";
                      (e.currentTarget as HTMLButtonElement).style.color = widgetUiColor;
                    }}
                  >
                    {item}
                  </button>
                ))}
              </div>
            ) : null}

            <div className="shrink-0 border-t p-3" style={{ backgroundColor: widgetBackgroundColor, borderTopColor: `${widgetUiColor}33`, paddingBottom: isMobile ? "max(0.75rem, env(safe-area-inset-bottom))" : undefined }}>
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  void sendMessage(input, "typed_input");
                }}
                className="flex items-end gap-2"
              >
                <textarea
                  ref={inputRef}
                  rows={1}
                  value={input}
                  onChange={(event) => setInput(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
                    event.preventDefault();
                    if (isSending || !input.trim() || agentHandoverStatus === "waiting" || inputGated) return;
                    void sendMessage(input, "typed_input");
                  }}
                  placeholder={
                    introFormRequired
                      ? "Please fill in your details above to begin..."
                      : formAwaitingCompletion || assistFormOpen
                        ? "Please complete the form above to continue..."
                        : locationPromptActive
                          ? "Please choose the city you want to stay in above to continue..."
                          : agentHandoverStatus === "waiting"
                            ? "Connecting you to an agent..."
                            : agentHandoverStatus === "agent_active"
                              ? `Reply to ${agentName || "agent"}...`
                              : "Type a message..."
                  }
                  disabled={agentHandoverStatus === "waiting" || inputGated}
                  className="max-h-24 min-h-[38px] flex-1 resize-none rounded border border-slate-300 px-2 py-2 text-sm disabled:opacity-60 disabled:cursor-not-allowed"
                  style={{ borderColor: `${widgetUiColor}66`, color: widgetTextColor, backgroundColor: "#ffffff" }}
                />
                <button
                  type="submit"
                  disabled={isSending || !input.trim() || agentHandoverStatus === "waiting" || inputGated}
                  aria-label="Send message"
                  className="flex h-10 w-10 items-center justify-center rounded-full text-white shadow-sm transition-opacity disabled:opacity-50"
                  style={{ backgroundColor: widgetUiColor }}
                >
                  <Send className="h-4 w-4" />
                </button>
              </form>
            </div>
                </div>
              )}
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {historyOpen ? (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 p-4">
          <div className="max-h-[80vh] w-full max-w-2xl overflow-auto rounded-lg bg-white p-4">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-base font-semibold">Session History</h2>
              <button type="button" onClick={() => setHistoryOpen(false)} className="rounded border px-2 py-1 text-sm">
                Close
              </button>
            </div>
            {historyLoading ? <p className="text-sm text-slate-600">Loading...</p> : null}
            {historyError ? <p className="text-sm text-rose-600">{historyError}</p> : null}
            {historyData ? (
              <div className="space-y-2 text-sm">
                <div className="rounded bg-slate-50 p-2">
                  <p>
                    <span className="font-medium">Session:</span> {historyData.session_id}
                  </p>
                  <p>
                    <span className="font-medium">State:</span> {historyData.state}
                  </p>
                  <p>
                    <span className="font-medium">Hotel:</span> {historyData.hotel_code}
                  </p>
                  <p>
                    <span className="font-medium">Created:</span> {new Date(historyData.created_at).toLocaleString()}
                  </p>
                </div>
                {(historyData.messages || []).map((row, index) => (
                  <div key={`${row.timestamp}_${index}`} className="rounded border border-slate-200 p-2">
                    <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">{row.role}</p>
                    <p className="mt-1 whitespace-pre-wrap">{row.content}</p>
                    <p className="mt-1 text-xs text-slate-500">{new Date(row.timestamp).toLocaleString()}</p>
                  </div>
                ))}
              </div>
            ) : null}
          </div>
        </div>
      ) : null}

      {ticketsOpen ? (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 p-4">
          <div className="max-h-[80vh] w-full max-w-3xl overflow-auto rounded-lg bg-white p-4">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-base font-semibold">Local Tickets</h2>
              <button type="button" onClick={() => setTicketsOpen(false)} className="rounded border px-2 py-1 text-sm">
                Close
              </button>
            </div>
            {ticketsLoading ? <p className="text-sm text-slate-600">Loading...</p> : null}
            {ticketsError ? <p className="text-sm text-rose-600">{ticketsError}</p> : null}
            {!ticketsLoading && !ticketsError && tickets.length === 0 ? (
              <p className="text-sm text-slate-600">No tickets yet.</p>
            ) : null}
            <div className="space-y-2">
              {tickets.map((row, index) => (
                <div key={`${row.ticket_id || row.id || index}`} className="rounded border border-slate-200 p-3 text-sm">
                  <div className="mb-1 flex items-center justify-between">
                    <strong>{String(row.ticket_id || row.id || "-")}</strong>
                    <span className="rounded bg-slate-900 px-2 py-0.5 text-xs text-white">{String(row.status || "open")}</span>
                  </div>
                  <pre className="max-h-36 overflow-auto whitespace-pre-wrap rounded bg-slate-50 p-2 text-xs text-slate-700">
                    {JSON.stringify(row, null, 2)}
                  </pre>
                </div>
              ))}
            </div>
          </div>
        </div>
      ) : null}

      {bookingModalOpen ? (
        <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-lg rounded-lg bg-white p-4">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-base font-semibold">Create Test Booking</h2>
              <button type="button" onClick={() => setBookingModalOpen(false)} className="rounded border px-2 py-1 text-sm">
                Close
              </button>
            </div>
            <form onSubmit={handleCreateBooking} className="grid gap-2">
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium text-slate-600">Guest Phone *</span>
                <input
                  required
                  className="rounded border border-slate-300 px-2 py-1.5 text-sm"
                  value={bookingDraft.guest_phone}
                  onChange={(event) => setBookingDraft((prev) => ({ ...prev, guest_phone: event.target.value }))}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium text-slate-600">Guest Name</span>
                <input
                  className="rounded border border-slate-300 px-2 py-1.5 text-sm"
                  value={bookingDraft.guest_name}
                  onChange={(event) => setBookingDraft((prev) => ({ ...prev, guest_name: event.target.value }))}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium text-slate-600">Property Name</span>
                <input
                  className="rounded border border-slate-300 px-2 py-1.5 text-sm"
                  value={bookingDraft.property_name}
                  onChange={(event) => setBookingDraft((prev) => ({ ...prev, property_name: event.target.value }))}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium text-slate-600">Room Number</span>
                <input
                  className="rounded border border-slate-300 px-2 py-1.5 text-sm"
                  value={bookingDraft.room_number}
                  onChange={(event) => setBookingDraft((prev) => ({ ...prev, room_number: event.target.value }))}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium text-slate-600">Room Type</span>
                <input
                  className="rounded border border-slate-300 px-2 py-1.5 text-sm"
                  value={bookingDraft.room_type}
                  onChange={(event) => setBookingDraft((prev) => ({ ...prev, room_type: event.target.value }))}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium text-slate-600">Check-in *</span>
                <input
                  required
                  type="date"
                  className="rounded border border-slate-300 px-2 py-1.5 text-sm"
                  value={bookingDraft.check_in_date}
                  onChange={(event) => setBookingDraft((prev) => ({ ...prev, check_in_date: event.target.value }))}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium text-slate-600">Check-out *</span>
                <input
                  required
                  type="date"
                  className="rounded border border-slate-300 px-2 py-1.5 text-sm"
                  value={bookingDraft.check_out_date}
                  onChange={(event) => setBookingDraft((prev) => ({ ...prev, check_out_date: event.target.value }))}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium text-slate-600">Guests</span>
                <input
                  min={1}
                  type="number"
                  className="rounded border border-slate-300 px-2 py-1.5 text-sm"
                  value={bookingDraft.num_guests}
                  onChange={(event) => setBookingDraft((prev) => ({ ...prev, num_guests: event.target.value }))}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs font-medium text-slate-600">Status</span>
                <select
                  className="rounded border border-slate-300 px-2 py-1.5 text-sm"
                  value={bookingDraft.status}
                  onChange={(event) => setBookingDraft((prev) => ({ ...prev, status: event.target.value }))}
                >
                  <option value="reserved">Reserved</option>
                  <option value="checked_in">Checked In</option>
                  <option value="checked_out">Checked Out</option>
                </select>
              </label>
              {bookingDraftError ? <p className="text-sm text-rose-600">{bookingDraftError}</p> : null}
              <div>
                <button
                  type="submit"
                  disabled={bookingDraftSubmitting}
                  className="rounded bg-slate-900 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                >
                  {bookingDraftSubmitting ? "Creating..." : "Create Booking"}
                </button>
              </div>
            </form>
          </div>
        </div>
      ) : null}
    </div>
  );
};

export default ChatHarness;

