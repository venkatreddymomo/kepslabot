/**
 * Guest Chat History — merged with Agent Handoff
 * KePSLA-inspired split pane with inline live agent chat.
 *
 * Taste: DESIGN_VARIANCE=2  MOTION_INTENSITY=1  VISUAL_DENSITY=7
 */

import {
  FormEvent, useState, useEffect, useCallback, useRef, useMemo,
} from "react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from "@/components/ui/dialog";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  Search, Phone, Calendar, RefreshCw, BedDouble,
  Building2, Globe, MessageSquare, ChevronDown, Hotel,
  Send, Bot, User, Headphones, Paperclip, Smile,
} from "lucide-react";
import {
  adminGet, getActivePropertyCode, setActivePropertyCode, normalizePropertyCode,
} from "@/lib/adminApi";
import { cn } from "@/lib/utils";
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  requestNotificationPermission,
  useAgentWebSocket,
  type PendingEscalation,
  type HistoryMessage,
  type ActiveSession,
} from "@/hooks/useAgentWebSocket";

// ─── Constants ────────────────────────────────────────────────────────────────

const ACTIVE_WINDOW_MS = 2 * 60 * 60 * 1000;
const REFRESH_INTERVAL = 30_000;
const DEPARTMENTS = ["Front Desk", "Housekeeping", "F&B", "Maintenance", "Management", "Guest Relations"];
const STAFF_BY_DEPT: Record<string, string[]> = {
  "Front Desk":      ["Priya Sharma", "Anita Desai"],
  "Housekeeping":    ["Rahul Verma", "Kavya Nair"],
  "F&B":             ["Vikram Patel", "Ravi Kumar"],
  "Maintenance":     ["Suresh Babu", "Mohammed Ali"],
  "Management":      ["Priya Sharma", "Vikram Patel"],
  "Guest Relations": ["Anita Desai", "Pooja Singh"],
};

// ─── Types ────────────────────────────────────────────────────────────────────

interface ChatSession {
  conversation_id: number;
  conversation_uid: string;   // rotating per-thread id (dustbin / 30-min idle) — the row identity
  session_id: string;
  state: string;
  is_active: boolean;   // now means "online right now" (live WS / active agent)
  is_live?: boolean;    // explicit live-presence flag from the backend
  is_recent?: boolean;  // last message within the 2-hour window (tab bucketing)
  channel: string;
  started_at: string;
  last_activity: string;
  message_count: number;
  user_turns: number;
  last_message: string;
  last_user_message: string;
  last_bot_message: string;
  guest_name: string;
  guest_phone: string;
  room_number: string;
  check_in_date: string;
  check_out_date: string;
  property_name: string;
  hotel_name: string;
  hotel_code: string;
  assigned_agent?: string;
}

interface ChatMessage {
  id: number;
  session_id: string;
  sender: string;
  role: string;
  message: string;
  // Rich display the guest actually saw (from new_bot_messages.display_payload).
  display_message?: string;
  images?: Array<Record<string, unknown>>;
  form_fields?: Array<Record<string, unknown>>;
  form_submission?: Record<string, string> | null;
  intent: string;
  confidence: number;
  timestamp: string;
  channel: string;
}

interface ConvMeta {
  state: string; channel: string; started_at: string;
  guest_name: string; guest_phone: string; room_number: string;
  check_in_date: string; check_out_date: string;
  property_name: string; hotel_name: string; hotel_code: string;
}

// Live agent chat message (added when agent joins)
interface LiveMessage {
  id: string;
  // "bot" is distinct from "agent": a live bot reply must NOT be mistaken for a
  // human agent handling the chat (that flipped the Bot/Agent control to "Agent").
  role: "guest" | "agent" | "system" | "bot";
  text: string;
  agentName?: string;
  timestamp: string;
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function computeIsActive(lastActivity: string): boolean {
  if (!lastActivity) return false;
  try { return Date.now() - new Date(lastActivity).getTime() < ACTIVE_WINDOW_MS; }
  catch { return false; }
}

function relTime(ts: string): string {
  if (!ts) return "";
  try {
    const diff = Date.now() - new Date(ts).getTime();
    const s = Math.floor(diff / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    const d = Math.floor(s / 86400);
    if (d === 1) return "Yesterday";
    if (d < 7) return new Date(ts).toLocaleDateString([], { weekday: "short" });
    return new Date(ts).toLocaleDateString([], { month: "short", day: "numeric" });
  } catch { return ""; }
}

function fmtTime(ts: string): string {
  if (!ts) return "";
  try { return new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
  catch { return ""; }
}

function fmtDate(ts: string): string {
  if (!ts) return "";
  try { return new Date(ts).toLocaleDateString([], { month: "short", day: "numeric", year: "numeric" }); }
  catch { return ts; }
}

function dateSepLabel(ts: string): string {
  if (!ts) return "";
  try {
    const d = new Date(ts);
    const now = new Date();
    if (d.toDateString() === now.toDateString()) return "Today";
    if (d.toDateString() === new Date(Date.now() - 86400000).toDateString()) return "Yesterday";
    return d.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
  } catch { return ts; }
}

function initials(name: string, fallback: string): string {
  const src = name?.trim() || fallback || "?";
  return src.trim().split(/\s+/).slice(0, 2).map(w => w[0]?.toUpperCase() ?? "").join("");
}

const PALETTE = [
  "bg-blue-100 text-blue-700",
  "bg-violet-100 text-violet-700",
  "bg-emerald-100 text-emerald-700",
  "bg-amber-100 text-amber-700",
];
function palette(id: number | string): string {
  let h = 0; const s = String(id);
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return PALETTE[Math.abs(h) % PALETTE.length];
}

// ─── Bot-message formatting (mirrors the widget so history reads identically) ──
// The chat widget renders bot replies as light markdown (bold/italic/headings)
// with auto-linked URLs. The history page used to print the raw text — which is
// why **double-asterisks** and bare links looked unformatted. This applies the
// same transform so a bot bubble here matches what the guest actually saw.

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
  } catch { return ""; }
}

function buildLinkHtml(url: string, label: string): string {
  const href = normalizeSafeLink(url);
  const text = String(label || url || "").trim();
  if (!href || !text) return escapeHtml(text || url || "");
  return `<a class="underline underline-offset-2 text-primary hover:opacity-80" href="${escapeHtml(
    href,
  )}" target="_blank" rel="noopener noreferrer">${escapeHtml(text)}</a>`;
}

function renderBotMarkdownToHtml(text: string): string {
  const escaped = escapeHtml(String(text || ""));
  if (!escaped) return "";
  let rendered = escaped;
  rendered = rendered.replace(/\*\*([\s\S]+?)\*\*/g, "<strong>$1</strong>");
  rendered = rendered.replace(/__([\s\S]+?)__/g, "<strong>$1</strong>");
  rendered = rendered.replace(/(?<!\w)\*((?!\s)[^*]+(?<!\s))\*(?!\w)/g, "<em>$1</em>");
  rendered = rendered.replace(/(?<!\w)_((?!\s)[^_]+(?<!\s))_(?!\w)/g, "<em>$1</em>");
  rendered = rendered.replace(/^#{1,3}\s+(.+)$/gm, "<strong>$1</strong>");
  rendered = rendered.replace(/\[([^\]\n]+?)\]\((https?:\/\/[^\s)]+)\)/g, (_m, label, url) =>
    buildLinkHtml(url, label),
  );
  rendered = rendered.replace(/(^|[\s(>])((?:https?:\/\/|www\.)[^\s<]+)/gi, (_m, prefix, url) => {
    const trailing = (url.match(/[).,!?:;]+$/) || [""])[0];
    const cleanUrl = trailing ? url.slice(0, -trailing.length) : url;
    return `${prefix}${buildLinkHtml(cleanUrl, cleanUrl)}${trailing}`;
  });
  return rendered.replace(/\n/g, "<br/>");
}

function hasAgentHandling(msgs: ChatMessage[]): boolean {
  return msgs.some(m =>
    m.role === "agent" || m.role === "human_agent" ||
    m.sender === "AGENT" || m.sender === "agent",
  );
}

function getAgentId(): string {
  const existing = localStorage.getItem("kebo_agent_id");
  if (existing) return existing;
  const id = `agent_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
  localStorage.setItem("kebo_agent_id", id);
  return id;
}

// ─── Sub-components ───────────────────────────────────────────────────────────

function ActiveDot({ active }: { active: boolean }) {
  return (
    <span className="inline-flex items-center gap-1 text-[11px] font-medium">
      <span className={cn("inline-block h-1.5 w-1.5 rounded-full",
        active ? "bg-green-500" : "bg-slate-300")} />
      <span className={active ? "text-green-600" : "text-muted-foreground"}>
        {active ? "Active" : "Closed"}
      </span>
    </span>
  );
}

function ChannelBadge({ channel }: { channel: string }) {
  const ch = channel?.toLowerCase() || "web";
  if (ch.includes("whatsapp")) {
    return <span className="text-[10px] font-semibold text-green-600 bg-green-50 px-1.5 py-0.5 rounded">WA</span>;
  }
  return (
    <span className="inline-flex items-center gap-0.5 text-[11px] text-muted-foreground">
      <Globe className="h-2.5 w-2.5" />web
    </span>
  );
}

function AgentBadge({ assigned }: { assigned?: string }) {
  if (!assigned) return null;
  return (
    <span className="inline-flex items-center gap-1 rounded-full border border-primary/20 bg-primary/10 px-2 py-0.5 text-[11px] font-medium text-primary">
      <User className="h-3 w-3" />
      Agent Handling
    </span>
  );
}

function ConvItem({ s, active, onClick }: { s: ChatSession; active: boolean; onClick: () => void }) {
  const isActive = s.is_active ?? computeIsActive(s.last_activity);
  const name = s.guest_name || `Session ...${s.session_id.slice(-6)}`;
  const preview = s.last_user_message || s.last_bot_message || "No messages yet";
  const hasAgent = !!s.assigned_agent;

  return (
    <button
      onClick={onClick}
      className={cn(
        "w-full text-left px-4 py-3 border-b border-border/50 transition-colors",
        "hover:bg-accent/60",
        active
          ? "bg-accent border-l-2 border-l-primary pl-[14px]"
          : "border-l-2 border-l-transparent",
      )}
    >
      <div className="flex items-center gap-3">
        <div className={cn(
          "h-9 w-9 rounded-full flex items-center justify-center text-xs font-bold flex-shrink-0",
          palette(s.conversation_id),
        )}>
          {initials(s.guest_name, s.session_id)}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-baseline justify-between gap-2">
            <span className="font-semibold text-[13px] leading-tight truncate">{name}</span>
            <span className="text-[11px] text-muted-foreground flex-shrink-0">{relTime(s.last_activity)}</span>
          </div>
          <div className="flex items-center gap-2 mt-0.5">
            <ActiveDot active={isActive} />
            <ChannelBadge channel={s.channel} />
            {s.room_number && (
              <span className="text-[11px] text-muted-foreground flex items-center gap-0.5">
                <BedDouble className="h-2.5 w-2.5" />Rm {s.room_number}
              </span>
            )}
          </div>
          <div className="flex items-center justify-between gap-1 mt-0.5">
            <p className="text-xs text-muted-foreground truncate flex-1">{preview}</p>
            {hasAgent ? (
              <span className="flex items-center gap-0.5 text-[10px] text-primary flex-shrink-0">
                <User className="h-2.5 w-2.5" />Agent
              </span>
            ) : (
              <span className="flex items-center gap-0.5 text-[10px] text-muted-foreground/60 flex-shrink-0">
                <Bot className="h-2.5 w-2.5" />Bot
              </span>
            )}
          </div>
        </div>
      </div>
    </button>
  );
}

function MsgBubble({ msg }: { msg: ChatMessage }) {
  const isUser = msg.role === "user" || msg.sender === "USER";
  const isAgent = msg.role === "agent" || msg.role === "human_agent" || msg.sender === "AGENT";
  const isSystem = msg.role === "system" || msg.sender === "SYSTEM";

  if (isSystem) {
    return (
      <div className="flex justify-center my-3">
        <span className="rounded-full bg-muted/60 px-3 py-1 text-[11px] text-muted-foreground">
          {msg.message}
        </span>
      </div>
    );
  }

  // Prefer the beautified text the guest actually saw; fall back to canonical.
  const botText = (msg.display_message || "").trim() || msg.message;
  const formSub = msg.form_submission && Object.keys(msg.form_submission).length > 0
    ? msg.form_submission
    : null;

  return (
    <div className={cn("flex items-end gap-2 mb-3", isUser ? "justify-start" : "justify-end")}>
      {isUser && (
        <span className="text-[10px] font-semibold text-muted-foreground flex-shrink-0 mb-1 w-7 text-center">User</span>
      )}
      <div className={cn("max-w-[68%] flex flex-col", isUser ? "items-start" : "items-end")}>
        {isAgent && (
          <p className="mb-0.5 text-[10px] font-medium text-primary px-1">Agent</p>
        )}
        <div className={cn(
          "rounded-2xl px-3.5 py-2.5 text-sm leading-relaxed",
          isUser
            ? "bg-muted text-foreground rounded-tl-sm"
            : isAgent
              ? "bg-primary/10 border border-primary/20 text-foreground rounded-tr-sm"
              : "bg-primary/10 text-foreground rounded-tr-sm",
        )}>
          {formSub
            // Submitted form values — rendered as a labeled block, like the widget.
            ? (
              <div className="flex flex-col gap-0.5">
                <span className="mb-0.5 text-[10px] font-semibold opacity-70">📋 Form submitted</span>
                {Object.entries(formSub).map(([k, v]) => (
                  <span key={k} className="text-[13px]">
                    <span className="font-medium">{k.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase())}:</span>{" "}
                    {v}
                  </span>
                ))}
              </div>
            )
            : !msg.message && !botText
              ? <em className="opacity-40 text-xs">empty</em>
              : isUser || isAgent
                // User text + agent text are shown verbatim (whitespace preserved).
                ? <span className="whitespace-pre-wrap">{msg.message}</span>
                // Bot replies render with the same light markdown the widget uses.
                : <span
                    className="[&_a]:break-words"
                    dangerouslySetInnerHTML={{ __html: renderBotMarkdownToHtml(botText) }}
                  />}
        </div>
        <div className={cn("flex items-center gap-2 mt-1 px-1", isUser ? "flex-row" : "flex-row-reverse")}>
          <span className="text-[10px] text-muted-foreground">{fmtTime(msg.timestamp)}</span>
          {!isUser && !isAgent && msg.intent && (
            <span className="text-[10px] text-muted-foreground italic">{msg.intent}</span>
          )}
        </div>
      </div>
      {!isUser && (
        <span className="text-[10px] font-semibold text-muted-foreground flex-shrink-0 mb-1 w-7 text-center">
          {isAgent ? "Agt" : "Bot"}
        </span>
      )}
    </div>
  );
}

function LiveMsgBubble({ msg, selfAgentName }: { msg: LiveMessage; selfAgentName: string }) {
  if (msg.role === "system") {
    return (
      <div className="flex justify-center my-3">
        <span className="rounded-full bg-muted/60 px-3 py-1 text-[11px] text-muted-foreground">{msg.text}</span>
      </div>
    );
  }
  const isSelf = msg.role === "agent" && !msg.agentName;
  const isGuest = msg.role === "guest";
  const isBot = msg.role === "bot";

  return (
    <div className={cn("flex items-end gap-2 mb-3", isGuest ? "justify-start" : "justify-end")}>
      {isGuest && (
        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-muted">
          <User className="h-3.5 w-3.5 text-muted-foreground" />
        </div>
      )}
      <div className="max-w-[72%]">
        {isBot ? (
          <p className="mb-0.5 text-right text-[10px] font-medium text-muted-foreground">Bot</p>
        ) : !isSelf && msg.agentName ? (
          <p className="mb-0.5 text-[10px] font-medium text-primary">{msg.agentName}</p>
        ) : null}
        <div className={cn(
          "rounded-2xl px-3.5 py-2.5 text-sm shadow-sm",
          isSelf
            ? "rounded-br-sm bg-primary text-primary-foreground"
            : isGuest
              ? "rounded-bl-sm bg-background border border-border text-foreground"
              : isBot
                ? "rounded-br-sm bg-primary/10 text-foreground"
                : "rounded-bl-sm bg-primary/10 border border-primary/20 text-foreground",
        )}>
          {isBot
            ? <span className="[&_a]:break-words" dangerouslySetInnerHTML={{ __html: renderBotMarkdownToHtml(msg.text) }} />
            : <p className="whitespace-pre-wrap">{msg.text}</p>}
        </div>
        <p className={cn("mt-0.5 text-[10px] text-muted-foreground/70", (isSelf || isBot) ? "text-right" : "")}>
          {fmtTime(msg.timestamp)}
        </p>
      </div>
    </div>
  );
}

function DateSep({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-3 my-5">
      <div className="flex-1 h-px bg-border/40" />
      <span className="text-[11px] text-muted-foreground font-medium">{label}</span>
      <div className="flex-1 h-px bg-border/40" />
    </div>
  );
}

// ─── Assign Modal ─────────────────────────────────────────────────────────────

function AssignModal({
  open, onClose, onAssign,
}: { open: boolean; onClose: () => void; onAssign: (dept: string, staff: string) => void }) {
  const [dept, setDept] = useState("");
  const [staff, setStaff] = useState("");
  const staffOptions = dept ? (STAFF_BY_DEPT[dept] ?? []) : [];

  const handleAssign = () => {
    if (!dept || !staff) return;
    onAssign(dept, staff);
    setDept(""); setStaff("");
    onClose();
  };

  return (
    <Dialog open={open} onOpenChange={onClose}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle className="text-base">Assign to Others</DialogTitle>
        </DialogHeader>
        <div className="space-y-4 py-2">
          <div className="space-y-1.5">
            <label className="text-sm font-medium">
              Department <span className="text-red-500">*</span>
            </label>
            <Select value={dept} onValueChange={v => { setDept(v); setStaff(""); }}>
              <SelectTrigger className="h-9 text-sm">
                <SelectValue placeholder="Select department" />
              </SelectTrigger>
              <SelectContent>
                {DEPARTMENTS.map(d => (
                  <SelectItem key={d} value={d} className="text-sm">{d}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <label className="text-sm font-medium">
              Staff <span className="text-red-500">*</span>
            </label>
            <Select value={staff} onValueChange={setStaff} disabled={!dept}>
              <SelectTrigger className="h-9 text-sm">
                <SelectValue placeholder={dept ? "Select a staff member" : "Select a department first"} />
              </SelectTrigger>
              <SelectContent>
                {staffOptions.map(s => (
                  <SelectItem key={s} value={s} className="text-sm">{s}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
        <DialogFooter className="gap-2">
          <Button variant="outline" size="sm" onClick={onClose}>Cancel</Button>
          <Button
            size="sm"
            disabled={!dept || !staff}
            onClick={handleAssign}
            className="bg-primary hover:bg-primary/90 text-primary-foreground"
          >
            Assign
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ─── Right Panel Header ───────────────────────────────────────────────────────

function ConvHeader({
  s, meta, messages, liveMessages, agentJoined, wsStatus,
  onJoinChat, onAssignBot, onOpenAssign, onTranscript,
}: {
  s: ChatSession;
  meta: ConvMeta | null;
  messages: ChatMessage[];
  liveMessages: LiveMessage[];
  agentJoined: boolean;
  wsStatus: string;
  onJoinChat: () => void;
  onAssignBot: () => void;
  onOpenAssign: () => void;
  onTranscript: () => void;
}) {
  const name = meta?.guest_name || s.guest_name || `Session ...${s.session_id.slice(-6)}`;
  const phone = meta?.guest_phone || s.guest_phone;
  const room = meta?.room_number || s.room_number;
  const ci = meta?.check_in_date || s.check_in_date;
  const co = meta?.check_out_date || s.check_out_date;
  const hotel = meta?.hotel_name || s.hotel_name;
  const channel = meta?.channel || s.channel;
  const isActive = s.is_active ?? computeIsActive(s.last_activity);
  const isAgentHandling = hasAgentHandling(messages) || liveMessages.some(m => m.role === "agent");

  return (
    <div className="flex-shrink-0 border-b bg-background px-5 py-3">
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-center gap-3 min-w-0">
          <div className={cn(
            "h-10 w-10 rounded-full flex items-center justify-center text-sm font-bold flex-shrink-0",
            palette(s.conversation_id),
          )}>
            {initials(s.guest_name, s.session_id)}
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h3 className="font-semibold text-sm leading-tight">{name}</h3>
              <ActiveDot active={isActive} />
              <ChannelBadge channel={channel} />
              {isAgentHandling && <AgentBadge assigned="agent" />}
            </div>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 mt-1">
              {phone && (
                <span className="text-xs text-muted-foreground flex items-center gap-1">
                  <Phone className="h-3 w-3" />{phone}
                </span>
              )}
              {room && (
                <span className="text-xs text-muted-foreground flex items-center gap-1">
                  <BedDouble className="h-3 w-3" />Room {room}
                </span>
              )}
              {ci && (
                <span className="text-xs text-muted-foreground flex items-center gap-1">
                  <Calendar className="h-3 w-3" />
                  {fmtDate(ci)}{co ? ` - ${fmtDate(co)}` : ""}
                </span>
              )}
              {hotel && (
                <span className="text-xs text-muted-foreground flex items-center gap-1">
                  <Building2 className="h-3 w-3" />{hotel}
                </span>
              )}
            </div>
          </div>
        </div>

        {/* Action buttons */}
        <div className="flex flex-shrink-0 items-center gap-2">
          {agentJoined && (
            <span className={cn(
              "h-2 w-2 rounded-full",
              wsStatus === "open" ? "bg-green-500 animate-pulse" : "bg-yellow-400 animate-pulse",
            )} title={wsStatus === "open" ? "Live" : "Connecting..."} />
          )}

          <Button
            size="sm" variant="outline"
            className="h-7 text-xs px-3"
            onClick={onTranscript}
          >
            Transcript
          </Button>

          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                size="sm"
                variant="outline"
                className={cn(
                  "h-7 text-xs px-3 gap-1",
                  agentJoined && "border-primary/40 text-primary",
                )}
              >
                {agentJoined ? (
                  <><Headphones className="h-3 w-3" />Agent</>
                ) : (
                  <><Bot className="h-3 w-3" />Bot</>
                )}
                <ChevronDown className="h-3 w-3 opacity-60" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-[170px]">
              {isActive && !agentJoined && (
                <DropdownMenuItem
                  className="text-xs cursor-pointer font-medium text-primary"
                  onClick={onJoinChat}
                >
                  <Headphones className="h-3.5 w-3.5 mr-2" />
                  Assign to Agent
                </DropdownMenuItem>
              )}
              {agentJoined && (
                <DropdownMenuItem
                  className="text-xs cursor-pointer"
                  onClick={onAssignBot}
                >
                  <Bot className="h-3.5 w-3.5 mr-2 text-muted-foreground" />
                  Assign to Bot
                </DropdownMenuItem>
              )}
              <DropdownMenuItem
                className="text-xs cursor-pointer"
                onClick={onOpenAssign}
              >
                <User className="h-3.5 w-3.5 mr-2 text-muted-foreground" />
                Assign to Others
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
    </div>
  );
}

// ─── Main Page ────────────────────────────────────────────────────────────────

const TABS = ["all", "active", "closed"] as const;
type Tab = typeof TABS[number];
interface PropertyOption { code: string; name: string; }

export default function GuestChatHistory() {
  // ── Session list state ──────────────────────────────────────────────────────
  const [sessions, setSessions]         = useState<ChatSession[]>([]);
  const [loading, setLoading]           = useState(true);
  const [selectedId, setSelectedId]     = useState<string | null>(null);  // session_id (drives WS/agent ops)
  const [selectedUid, setSelectedUid]   = useState<string | null>(null);  // conversation_uid (the selected THREAD)
  const [messages, setMessages]         = useState<ChatMessage[]>([]);
  const [convMeta, setConvMeta]         = useState<ConvMeta | null>(null);
  const [loadingMsgs, setLoadingMsgs]   = useState(false);
  const [search, setSearch]             = useState("");
  const [debSearch, setDebSearch]       = useState("");
  const [dateFrom, setDateFrom]         = useState("");
  const [dateTo, setDateTo]             = useState("");
  const [tab, setTab]                   = useState<Tab>("all");
  const [channel, setChannel]           = useState("all");
  const [propertyCode, setPropertyCode] = useState<string>(getActivePropertyCode());
  const [propertyOptions, setPropertyOptions] = useState<PropertyOption[]>([]);
  const bottomRef = useRef<HTMLDivElement>(null);

  // ── Agent / Live state ─────────────────────────────────────────────────────
  const [agentId]                       = useState<string>(getAgentId);
  const [agentName]                     = useState<string>(() => localStorage.getItem("kebo_agent_name") || "Agent");
  const [agentJoined, setAgentJoined]   = useState(false);
  const [liveMessages, setLiveMessages] = useState<LiveMessage[]>([]);
  const [inputText, setInputText]       = useState("");
  const [guestTyping, setGuestTyping]   = useState(false);
  const inputRef                        = useRef<HTMLTextAreaElement>(null);

  // ── Assign modal ───────────────────────────────────────────────────────────
  const [assignOpen, setAssignOpen] = useState(false);

  // ── Selected session ───────────────────────────────────────────────────────
  // Resolve the exact THREAD by its conversation_uid (a session can have many).
  const selectedSession =
    sessions.find(s => s.conversation_uid === selectedUid)
    ?? sessions.find(s => s.session_id === selectedId)
    ?? null;
  const selectedHotelCode = selectedSession?.hotel_code || propertyCode;

  // selectedId ref — always current inside WS callbacks without causing reconnects
  const selectedIdRef = useRef<string | null>(null);
  useEffect(() => { selectedIdRef.current = selectedId; }, [selectedId]);

  // ── WebSocket callbacks ────────────────────────────────────────────────────

  const onGuestMessage = useCallback((sessionId: string, text: string, timestamp: string) => {
    // Update session list live regardless of which session is selected
    setSessions(prev => prev.map(s =>
      s.session_id === sessionId
        ? { ...s, last_activity: timestamp, last_user_message: text, is_active: true }
        : s,
    ));
    if (sessionId !== selectedIdRef.current) return;
    setLiveMessages(prev => [...prev, { id: `g_${Date.now()}`, role: "guest", text, timestamp }]);
    setGuestTyping(false);
  }, []);

  const onGuestTyping = useCallback((sessionId: string) => {
    if (sessionId !== selectedIdRef.current) return;
    setGuestTyping(true);
    setTimeout(() => setGuestTyping(false), 3000);
  }, []);

  // Real-time bot/guest messages for monitored sessions — append directly to message stream
  const onSessionMessage = useCallback((sessionId: string, role: "guest" | "bot", text: string, timestamp: string) => {
    if (sessionId !== selectedIdRef.current) return;
    setLiveMessages(prev => [...prev, {
      id: `sm_${Date.now()}`, role: role === "bot" ? "bot" : "guest", text, timestamp,
    }]);
    // Also keep session list fresh (update last_activity so active dot stays correct)
    setSessions(prev => prev.map(s =>
      s.session_id === sessionId
        ? { ...s, last_activity: timestamp, last_message: text, is_active: true }
        : s,
    ));
  }, []);

  // Session ended — immediately mark closed in list (no wait for 30s refresh)
  const onSessionEnded = useCallback((sessionId: string) => {
    setSessions(prev => prev.map(s =>
      s.session_id === sessionId ? { ...s, is_active: false } : s,
    ));
    if (sessionId === selectedIdRef.current) {
      setLiveMessages(prev => [...prev, {
        id: `sys_${Date.now()}`, role: "system",
        text: "Guest has left the conversation.",
        timestamp: new Date().toISOString(),
      }]);
      setAgentJoined(false);
    }
  }, []);

  // New session started — add to list immediately
  const onSessionStarted = useCallback((session: ActiveSession) => {
    setSessions(prev => {
      if (prev.some(s => s.session_id === session.session_id)) return prev;
      const newSession: ChatSession = {
        conversation_id: Date.now(),
        // Placeholder thread id until the next list refresh replaces it with the
        // real rotating conversation_uid from the backend.
        conversation_uid: `live:${session.session_id}`,
        session_id: session.session_id,
        state: "active",
        is_active: true,
        channel: "web",
        started_at: session.started_at,
        last_activity: session.started_at,
        message_count: 0,
        user_turns: 0,
        last_message: "",
        last_user_message: "",
        last_bot_message: "",
        guest_name: session.guest_info?.guest_name || "",
        guest_phone: session.guest_info?.guest_phone || "",
        room_number: session.guest_info?.room_number || "",
        check_in_date: "",
        check_out_date: "",
        property_name: "",
        hotel_name: "",
        hotel_code: session.guest_info?.hotel_code || "",
      };
      return [newSession, ...prev];
    });
  }, []);

  const onAccepted = useCallback((sessionId: string, _guestInfo: PendingEscalation["guest_info"]) => {
    if (sessionId !== selectedIdRef.current) return;
    setAgentJoined(true);
    setLiveMessages(prev => [...prev, {
      id: `sys_${Date.now()}`, role: "system",
      text: "You are now handling this conversation.",
      timestamp: new Date().toISOString(),
    }]);
    // Mark as agent-handled in list
    setSessions(prev => prev.map(s =>
      s.session_id === sessionId ? { ...s, assigned_agent: agentName } : s,
    ));
  }, [agentName]);

  // History replay is intentionally a no-op here. This page already loads the
  // full per-session thread from /engage-sessions/{id} (the DB), and the live
  // stream appends only NEW messages that arrive after selection. Re-injecting
  // the agent-WS history replay duplicated the whole conversation (and fired
  // twice — once on monitor, once on takeover), producing the doubled
  // "Conversation history (N messages)" blocks. One session = one thread.
  const onHistory = useCallback((_sessionId: string, _history: HistoryMessage[]) => {
    /* no-op: DB thread is the single source for this page */
  }, []);

  const onResolved = useCallback((sessionId: string) => {
    if (sessionId !== selectedIdRef.current) return;
    setLiveMessages(prev => [...prev, {
      id: `sys_${Date.now()}`, role: "system",
      text: "Conversation resolved. Bot has resumed.",
      timestamp: new Date().toISOString(),
    }]);
    setTimeout(() => setAgentJoined(false), 1500);
  }, []);

  const onAgentMessageEcho = useCallback((sessionId: string, text: string, echoName: string, timestamp: string) => {
    if (sessionId !== selectedIdRef.current || echoName === agentName) return;
    setLiveMessages(prev => [...prev, {
      id: `ae_${Date.now()}`, role: "agent", text, agentName: echoName, timestamp,
    }]);
  }, [agentName]);

  // WS always connected — property-scoped, receives all live events
  const {
    status: wsStatus,
    sendMessage: wsSend,
    sendResolve,
    sendMonitor,
    sendTakeover,
  } = useAgentWebSocket({
    hotelCode: propertyCode,
    agentId,
    agentName,
    onNewEscalation:      useCallback(() => {}, []),
    onGuestMessage,
    onGuestTyping,
    onEscalationAccepted: useCallback(() => {}, []),
    onAccepted,
    onObserving:          useCallback(() => {}, []),
    onObserverJoined:     useCallback(() => {}, []),
    onResolved,
    onEscalationExpired:  useCallback(() => {}, []),
    onHistory,
    onAgentMessageEcho,
    onActiveSession:      useCallback(() => {}, []),
    onSessionStarted,
    onSessionEnded,
    onSessionMessage,
    onMonitoring:         useCallback(() => {}, []),
    onSessionTakenOver:   useCallback(() => {}, []),
  });

  // ── Load properties ─────────────────────────────────────────────────────────
  useEffect(() => {
    adminGet<{ properties?: Array<{ id?: string; code?: string; name?: string }> }>("/properties")
      .then(payload => {
        const rows = Array.isArray(payload?.properties)
          ? payload.properties
              .map(row => ({
                code: normalizePropertyCode(String(row?.id || row?.code || "")),
                name: String(row?.name || row?.code || "").trim(),
              }))
              .filter(r => r.code)
          : [];
        if (rows.length > 0) setPropertyOptions(rows);
      })
      .catch(() => {});
  }, []);

  // Request notification permission (for agent alerts)
  useEffect(() => { requestNotificationPermission().catch(() => {}); }, []);

  // Debounce search
  useEffect(() => {
    const t = setTimeout(() => setDebSearch(search), 380);
    return () => clearTimeout(t);
  }, [search]);

  // Auto-scroll to bottom
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, liveMessages, guestTyping]);

  // ── Fetch sessions ──────────────────────────────────────────────────────────
  const fetchSessions = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const p = new URLSearchParams({ limit: "300", offset: "0" });
      if (debSearch) p.set("search", debSearch);
      if (dateFrom)  p.set("date_from", dateFrom);
      if (dateTo)    p.set("date_to",   dateTo);
      if (tab !== "all") p.set("state", tab);
      if (channel !== "all") p.set("channel", channel);
      const data = await adminGet<{ sessions: ChatSession[] }>(`/engage-sessions?${p}`, propertyCode);
      setSessions(data.sessions ?? []);
    } catch {
      if (!silent) setSessions([]);
    } finally {
      if (!silent) setLoading(false);
    }
  }, [debSearch, dateFrom, dateTo, tab, propertyCode, channel]);

  useEffect(() => { fetchSessions(); }, [fetchSessions]);
  useEffect(() => {
    const id = setInterval(() => fetchSessions(true), REFRESH_INTERVAL);
    return () => clearInterval(id);
  }, [fetchSessions]);

  // ── Select a session ────────────────────────────────────────────────────────
  const fetchSessionMessages = useCallback(async (sid: string, uid?: string | null) => {
    setMessages([]); setConvMeta(null); setLoadingMsgs(true);
    try {
      // Scope the transcript to the selected THREAD when we have its uid, so the
      // dustbin / 30-min-idle threads show as separate chats (not one big dump).
      const q = uid ? `?conversation_uid=${encodeURIComponent(uid)}` : "";
      const data = await adminGet<{ messages: ChatMessage[]; conversation: ConvMeta }>(
        `/engage-sessions/${encodeURIComponent(sid)}${q}`,
      );
      setMessages(data.messages ?? []);
      setConvMeta(data.conversation ?? null);
    } catch {
      setMessages([]);
    } finally {
      setLoadingMsgs(false);
    }
  }, []);

  const selectSession = useCallback(async (session: ChatSession) => {
    const uid = session.conversation_uid;
    const sid = session.session_id;
    // Session-wise history page: identity is the session_id (conversation_uid is
    // blank for DB rows now). Guard on sid so clicking a different session always
    // loads it (an empty-uid === empty-uid check would wrongly no-op).
    if (sid === selectedId && uid === selectedUid) return;
    setSelectedUid(uid);
    setSelectedId(sid);
    setAgentJoined(false);
    setLiveMessages([]);
    setInputText("");
    setGuestTyping(false);
    await fetchSessionMessages(sid, uid);
    // Subscribe to live messages for active sessions (no agent takeover, read-only)
    const isActive = session.is_active ?? computeIsActive(session.last_activity ?? "");
    if (isActive && wsStatus === "open" && sid) {
      sendMonitor(sid);
    }
  }, [selectedUid, selectedId, fetchSessionMessages, wsStatus, sendMonitor]);

  // When WS connects after a session is already selected, start monitoring
  useEffect(() => {
    if (wsStatus !== "open" || !selectedId || agentJoined) return;
    const session = sessions.find(s => s.session_id === selectedId);
    const isActive = session?.is_active ?? computeIsActive(session?.last_activity ?? "");
    if (isActive) sendMonitor(selectedId);
  }, [wsStatus]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Agent actions ───────────────────────────────────────────────────────────
  // "Assign to Agent" = this agent takes over the bot session
  const handleJoinChat = useCallback(() => {
    if (!selectedId) return;
    sendTakeover(selectedId);
  }, [selectedId, sendTakeover]);

  // "Assign to Bot" = resolve / hand back to bot
  const handleAssignBot = useCallback(() => {
    if (!selectedId) return;
    sendResolve(selectedId);
    setAgentJoined(false);
  }, [selectedId, sendResolve]);

  const handleAssignOther = useCallback((dept: string, staff: string) => {
    if (!selectedId) return;
    const sysMsg: LiveMessage = {
      id: `sys_${Date.now()}`, role: "system",
      text: `Assigned to ${staff} (${dept})`,
      timestamp: new Date().toISOString(),
    };
    setLiveMessages(prev => [...prev, sysMsg]);
    // Update sessions list to reflect assignment
    setSessions(prev => prev.map(s =>
      s.session_id === selectedId ? { ...s, assigned_agent: staff } : s,
    ));
  }, [selectedId]);

  const handleTranscript = useCallback(() => {
    if (!selectedId) return;
    const lines = messages.map(m => `[${fmtTime(m.timestamp)}] ${m.role.toUpperCase()}: ${m.message}`);
    const blob = new Blob([lines.join("\n")], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `transcript_${selectedId.slice(-8)}.txt`;
    a.click();
  }, [selectedId, messages]);

  // ── Send message ────────────────────────────────────────────────────────────
  const handleSend = useCallback((e?: FormEvent) => {
    e?.preventDefault();
    const text = inputText.trim();
    if (!text || !selectedId || !agentJoined) return;
    wsSend(selectedId, text);
    setLiveMessages(prev => [...prev, {
      id: `a_${Date.now()}`, role: "agent", text,
      timestamp: new Date().toISOString(),
    }]);
    setInputText("");
    setTimeout(() => inputRef.current?.focus(), 0);
  }, [inputText, selectedId, agentJoined, wsSend]);

  // ── Counts ─────────────────────────────────────────────────────────────────
  const counts = useMemo(() => {
    const all = sessions.length;
    const active = sessions.filter(s => s.is_active ?? computeIsActive(s.last_activity)).length;
    return { all, active, closed: all - active };
  }, [sessions]);

  // ── Message list with date separators ──────────────────────────────────────
  type ListItem =
    | { type: "sep"; label: string; key: string }
    | { type: "msg"; msg: ChatMessage; key: string };

  const msgItems = useMemo<ListItem[]>(() => {
    const items: ListItem[] = [];
    let lastLabel = "";
    for (const msg of messages) {
      const lbl = dateSepLabel(msg.timestamp);
      if (lbl !== lastLabel) {
        items.push({ type: "sep", label: lbl, key: `sep-${lbl}` });
        lastLabel = lbl;
      }
      items.push({ type: "msg", msg, key: `msg-${msg.id}` });
    }
    return items;
  }, [messages]);

  const hasFilters = !!(debSearch || dateFrom || dateTo || channel !== "all");

  // ── Render ──────────────────────────────────────────────────────────────────
  return (
    <div className="flex flex-1 flex-col overflow-hidden min-h-0">
      <AssignModal
        open={assignOpen}
        onClose={() => setAssignOpen(false)}
        onAssign={handleAssignOther}
      />

      {/* Header */}
      <div className="flex-shrink-0 flex items-center justify-between px-6 py-3 border-b">
        <div className="flex items-center gap-3">
          <h1 className="text-base font-bold tracking-tight">Guest Chat History</h1>
          <span className="text-xs text-muted-foreground">
            {counts.all} conversations{" "}·{" "}
            <span className="text-green-600">{counts.active} active</span>
          </span>
        </div>
        <div className="flex items-center gap-2">
          {propertyOptions.length > 0 && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="outline" size="sm" className="gap-1.5 text-xs h-8 max-w-[180px]">
                  <Hotel className="h-3.5 w-3.5 flex-shrink-0" />
                  <span className="truncate">
                    {propertyOptions.find(o => o.code === propertyCode)?.name || propertyCode}
                  </span>
                  <ChevronDown className="h-3 w-3 flex-shrink-0 opacity-60" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="min-w-[180px]">
                {propertyOptions.map(opt => (
                  <DropdownMenuItem
                    key={opt.code}
                    className={cn("text-xs cursor-pointer", propertyCode === opt.code && "font-semibold text-primary")}
                    onClick={() => {
                      const code = setActivePropertyCode(opt.code);
                      setPropertyCode(code);
                      setSelectedId(null);
                    }}
                  >
                    {opt.name}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          )}
          <Button
            variant="outline" size="sm"
            onClick={() => { fetchSessions(); if (selectedId) fetchSessionMessages(selectedId, selectedUid); }}
            disabled={loading}
            className="gap-1.5 text-xs"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
            Refresh
          </Button>
        </div>
      </div>

      {/* Filter bar */}
      <div className="flex-shrink-0 flex flex-wrap items-center gap-3 px-6 py-3 border-b bg-background">
        <div className="relative">
          <Search className="absolute left-2.5 top-2 h-3.5 w-3.5 text-muted-foreground" />
          <Input
            placeholder="Search name, phone, session..."
            className="pl-8 h-8 text-xs w-52"
            value={search}
            onChange={e => setSearch(e.target.value)}
          />
        </div>
        <div className="flex items-center gap-2 border rounded-md px-3 py-1.5 bg-muted/20">
          <span className="text-[11px] font-medium text-muted-foreground whitespace-nowrap">From</span>
          <input type="date" className="text-xs bg-transparent border-none outline-none text-foreground w-28 cursor-pointer"
            value={dateFrom} onChange={e => setDateFrom(e.target.value)} />
          <span className="text-muted-foreground/60">-&gt;</span>
          <input type="date" className="text-xs bg-transparent border-none outline-none text-foreground w-28 cursor-pointer"
            value={dateTo} onChange={e => setDateTo(e.target.value)} />
          {(dateFrom || dateTo) && (
            <button className="text-[11px] text-muted-foreground hover:text-foreground ml-1"
              onClick={() => { setDateFrom(""); setDateTo(""); }}>x</button>
          )}
        </div>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button className="flex items-center gap-1 h-7 px-2.5 text-xs border rounded-md bg-background hover:bg-muted/50 transition-colors font-medium">
              {channel === "all" ? "All Channels" : channel === "web" ? "Web" : "WhatsApp"}
              <ChevronDown className="h-3 w-3 opacity-60 ml-0.5" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="min-w-[130px]">
            {[
              { value: "all", label: "All Channels" },
              { value: "web", label: "Web" },
              { value: "whatsapp", label: "WhatsApp" },
            ].map(opt => (
              <DropdownMenuItem
                key={opt.value}
                className={cn("text-xs cursor-pointer", channel === opt.value && "font-semibold text-primary")}
                onSelect={() => setChannel(opt.value)}
              >
                {opt.label}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
        {hasFilters && (
          <Button variant="ghost" size="sm" className="h-8 text-xs px-2 text-muted-foreground"
            onClick={() => { setSearch(""); setDateFrom(""); setDateTo(""); setChannel("all"); }}>
            Clear all
          </Button>
        )}
      </div>

      {/* Split pane */}
      <div className="flex flex-1 overflow-hidden min-h-0">

        {/* Left panel */}
        <div className="w-72 xl:w-80 flex-shrink-0 flex flex-col border-r overflow-hidden">
          <div className="flex border-b flex-shrink-0">
            {TABS.map(t => {
              const label = t === "all" ? `All (${counts.all})`
                : t === "active" ? `Active (${counts.active})`
                : `Closed (${counts.closed})`;
              return (
                <button key={t} onClick={() => setTab(t)}
                  className={cn(
                    "flex-1 py-2 text-xs font-medium transition-colors border-b-2 -mb-px",
                    tab === t
                      ? "border-primary text-primary"
                      : "border-transparent text-muted-foreground hover:text-foreground",
                  )}
                >
                  {label}
                </button>
              );
            })}
          </div>
          <ScrollArea className="flex-1">
            {loading ? (
              <div className="p-4 space-y-4">
                {Array.from({ length: 8 }).map((_, i) => (
                  <div key={i} className="flex gap-3">
                    <Skeleton className="h-9 w-9 rounded-full flex-shrink-0" />
                    <div className="flex-1 space-y-1.5">
                      <Skeleton className="h-3 w-3/4" />
                      <Skeleton className="h-2.5 w-1/2" />
                      <Skeleton className="h-2.5 w-full" />
                    </div>
                  </div>
                ))}
              </div>
            ) : sessions.length === 0 ? (
              <div className="flex flex-col items-center justify-center h-48 text-muted-foreground gap-2">
                <MessageSquare className="h-8 w-8 opacity-20" />
                <p className="text-xs">{hasFilters ? "No results - try clearing filters" : "No conversations yet"}</p>
              </div>
            ) : (
              sessions.map(s => (
                <ConvItem
                  key={s.conversation_uid || s.session_id || s.conversation_id}
                  s={s}
                  active={(s.session_id && s.session_id === selectedId) || (!!s.conversation_uid && s.conversation_uid === selectedUid)}
                  onClick={() => selectSession(s)}
                />
              ))
            )}
          </ScrollArea>
        </div>

        {/* Right panel */}
        <div className="flex-1 flex flex-col overflow-hidden min-w-0">
          {selectedSession ? (
            <>
              <ConvHeader
                s={selectedSession}
                meta={convMeta}
                messages={messages}
                liveMessages={liveMessages}
                agentJoined={agentJoined}
                wsStatus={wsStatus}
                onJoinChat={handleJoinChat}
                onAssignBot={handleAssignBot}
                onOpenAssign={() => setAssignOpen(true)}
                onTranscript={handleTranscript}
              />

              {/* Agent joined banner */}
              {agentJoined && (
                <div className="flex-shrink-0 border-b border-primary/10 bg-primary/5 px-5 py-2">
                  <p className="text-xs text-primary/80">
                    <Headphones className="inline h-3.5 w-3.5 mr-1" />
                    You are live as <strong>{agentName}</strong>. Use Bot &gt; Assign to Bot to hand back.
                  </p>
                </div>
              )}

              <ScrollArea className="flex-1 px-5 py-4">
                {loadingMsgs ? (
                  <div className="space-y-4">
                    {[false, true, false, true, false, true].map((right, i) => (
                      <div key={i} className={cn("flex gap-2 items-end", right ? "justify-end" : "justify-start")}>
                        {!right && <Skeleton className="h-5 w-7 rounded flex-shrink-0" />}
                        <Skeleton className={cn("h-10 rounded-2xl", right ? "w-48" : "w-56")} />
                        {right && <Skeleton className="h-5 w-7 rounded flex-shrink-0" />}
                      </div>
                    ))}
                  </div>
                ) : (
                  <>
                    {/* Historical messages */}
                    {msgItems.map(item =>
                      item.type === "sep"
                        ? <DateSep key={item.key} label={item.label} />
                        : <MsgBubble key={item.key} msg={item.msg} />
                    )}

                    {/* Live messages — continuous stream, no separator */}
                    {liveMessages.map(msg => (
                      <LiveMsgBubble key={msg.id} msg={msg} selfAgentName={agentName} />
                    ))}

                    {/* Guest typing indicator */}
                    {guestTyping && (
                      <div className="flex items-end justify-start gap-2 mb-3">
                        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-muted">
                          <User className="h-3.5 w-3.5 text-muted-foreground" />
                        </div>
                        <div className="flex items-center gap-1.5 rounded-2xl rounded-bl-sm border bg-background px-3.5 py-2.5">
                          <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground/50 [animation-delay:-0.3s]" />
                          <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground/50 [animation-delay:-0.15s]" />
                          <span className="h-1.5 w-1.5 animate-bounce rounded-full bg-muted-foreground/50" />
                        </div>
                      </div>
                    )}

                    {msgItems.length === 0 && liveMessages.length === 0 && !loadingMsgs && (
                      <div className="flex flex-col items-center justify-center h-40 gap-2 text-muted-foreground">
                        <MessageSquare className="h-8 w-8 opacity-20" />
                        <p className="text-xs">No messages in this session</p>
                      </div>
                    )}
                    <div ref={bottomRef} />
                  </>
                )}
              </ScrollArea>

              {/* Message input — only when agent joined */}
              {agentJoined && (
                <div className="flex-shrink-0 border-t bg-background px-4 py-3">
                  <form onSubmit={handleSend} className="flex items-end gap-2">
                    <button type="button" className="flex-shrink-0 text-muted-foreground hover:text-foreground p-1.5 rounded-lg hover:bg-muted/50 transition-colors">
                      <Smile className="h-5 w-5" />
                    </button>
                    <button type="button" className="flex-shrink-0 text-muted-foreground hover:text-foreground p-1.5 rounded-lg hover:bg-muted/50 transition-colors">
                      <Paperclip className="h-5 w-5" />
                    </button>
                    <textarea
                      ref={inputRef}
                      rows={1}
                      value={inputText}
                      onChange={e => setInputText(e.target.value)}
                      onKeyDown={e => {
                        if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
                        e.preventDefault();
                        handleSend();
                      }}
                      placeholder="Type a message..."
                      className="flex-1 max-h-24 min-h-[38px] resize-none rounded-lg border border-border px-3 py-2 text-sm focus:border-primary focus:outline-none"
                    />
                    <button
                      type="submit"
                      disabled={!inputText.trim()}
                      className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground shadow-sm transition-opacity hover:bg-primary/90 disabled:opacity-40"
                    >
                      <Send className="h-4 w-4" />
                    </button>
                  </form>
                </div>
              )}
            </>
          ) : (
            <div className="flex flex-col items-center justify-center flex-1 gap-3 text-muted-foreground">
              <div className="rounded-full bg-muted/30 p-5">
                <MessageSquare className="h-10 w-10 opacity-20" />
              </div>
              <p className="text-sm font-medium text-foreground/60">Select a conversation</p>
              <p className="text-xs opacity-50">Click any session on the left to view the thread</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
