/**
 * TicketList — Guest Ticket Management (KePSLA layout)
 *
 * Layout matches the Kepsla-style screenshots:
 *   - Top bar: Search · date pills · property selector · Columns toggle · "+ Create Ticket"
 *   - Stat cards (Total / Open / In Progress / Closed / Escalated / Cancelled) with date subtitle
 *   - Inline filter dropdowns inside the column headers (Stage / Priority / Department / Status)
 *   - Status filter is multi-select (checkboxes + Apply)
 *   - Department options are the 4 fixed labels the LLM classifies into
 */

import {
  useState, useEffect, useCallback, useMemo,
} from "react";
import { Input }    from "@/components/ui/input";
import { Button }   from "@/components/ui/button";
import {
  Search, RefreshCw, Plus, Ticket, AlertTriangle,
  CheckCircle2, Clock, XCircle, TrendingUp,
  ChevronDown, MoreHorizontal, Pencil, Eye, Hotel, Columns3,
} from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Dialog, DialogContent, DialogHeader,
  DialogTitle, DialogFooter,
} from "@/components/ui/dialog";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Checkbox } from "@/components/ui/checkbox";
import {
  adminGet, adminSend, getActivePropertyCode, setActivePropertyCode, normalizePropertyCode,
} from "@/lib/adminApi";
import { cn } from "@/lib/utils";

// ─── Types ─────────────────────────────────────────────────────────────────

interface TicketRow {
  ticket_db_id:   number;
  ticket_ref:     string;
  session_id:     string | null;
  conversation_uid: string | null;
  guest_name:     string;
  guest_id:       string | null;
  room_number:    string | null;
  hotel_code:     string | null;
  property_name:  string | null;
  source:         string;
  issue:          string;
  description:    string | null;
  category:       string;
  sub_category:   string | null;
  phase:          string | null;
  priority:       string;
  status:         string;
  department:     string | null;
  assignee:       string | null;
  is_escalated:   boolean;
  escalation_stage: string | null;
  manager_notes:  string | null;
  sentiment_score: string | null;
  sla_due_at:     string | null;
  closed_at:      string | null;
  created_at:     string | null;
  updated_at:     string | null;
}

interface Stats {
  total: number; open: number; in_progress: number;
  closed: number; escalated: number; cancelled: number;
}

interface Option { value: string; label: string }

// ─── Constants ──────────────────────────────────────────────────────────────

const PHASES: Option[] = [
  { value: "all",          label: "All" },
  { value: "booking",      label: "Booking" },
  { value: "pre_checkin",  label: "Pre Checkin" },
  { value: "during_stay",  label: "During Stay" },
  { value: "post_checkout",label: "Post Checkout" },
];

const PRIORITIES: Option[] = [
  { value: "all",      label: "All" },
  { value: "low",      label: "Low" },
  { value: "medium",   label: "Medium" },
  { value: "high",     label: "High" },
  { value: "critical", label: "Critical" },
];

// The 4 KePSLA-side departments the LLM classifies tickets into.
const DEPARTMENTS: Option[] = [
  { value: "all",                     label: "All" },
  { value: "Reception Front Office",  label: "Reception Front Office" },
  { value: "Guest Relations",         label: "Guest Relations" },
  { value: "Housekeeping",            label: "Housekeeping" },
  { value: "In-Room Dining",          label: "In-Room Dining" },
];

// Multi-select status options. "all" pseudo-row toggles every status.
const STATUS_OPTIONS: Option[] = [
  { value: "open",        label: "Open" },
  { value: "in_progress", label: "In Progress" },
  { value: "closed",      label: "Closed" },
  { value: "cancelled",   label: "Cancel" },
];

// Edit-dialog status set (single-select with all options).
const EDIT_STATUSES: Option[] = [
  { value: "open",        label: "Open" },
  { value: "in_progress", label: "In Progress" },
  { value: "closed",      label: "Closed" },
  { value: "escalated",   label: "Escalated" },
  { value: "cancelled",   label: "Cancelled" },
];

const PHASE_LABELS: Record<string, string> = {
  booking: "Booking", pre_checkin: "Pre Checkin",
  during_stay: "During Stay", post_checkout: "Post Checkout",
};

// Theme-aligned pill styles. Primary tokens drive the pink/crimson accent;
// neutrals lean on muted-foreground so nothing competes with the brand color.
const STATUS_STYLES: Record<string, string> = {
  open:        "bg-primary/10 text-primary border border-primary/30",
  in_progress: "bg-muted      text-foreground/80 border border-border",
  closed:      "bg-muted/60   text-muted-foreground border border-border/60",
  escalated:   "bg-destructive/10 text-destructive border border-destructive/30",
  cancelled:   "bg-muted/40   text-muted-foreground/70 border border-border/40",
};

const PRIORITY_STYLES: Record<string, string> = {
  low:      "bg-muted/60   text-muted-foreground",
  medium:   "bg-muted      text-foreground/80",
  high:     "bg-primary/10 text-primary",
  critical: "bg-destructive/10 text-destructive font-semibold",
};

// Column registry — used by the Columns(N) toggle.
type ColumnKey =
  | "ticket_ref" | "room_number" | "property_name" | "guest_name"
  | "description" | "phase" | "priority" | "department" | "status"
  | "assignee" | "is_escalated" | "created_at" | "type" | "actions";

const COLUMN_DEFS: { key: ColumnKey; label: string; defaultVisible: boolean }[] = [
  { key: "ticket_ref",    label: "Ticket ID",     defaultVisible: true  },
  { key: "room_number",   label: "Room No",       defaultVisible: true  },
  { key: "property_name", label: "Property Name", defaultVisible: true  },
  { key: "guest_name",    label: "Guest Name",    defaultVisible: true  },
  { key: "description",   label: "Description",   defaultVisible: true  },
  { key: "phase",         label: "Stage",         defaultVisible: true  },
  { key: "priority",      label: "Priority",      defaultVisible: true  },
  { key: "department",    label: "Department",    defaultVisible: true  },
  { key: "status",        label: "Status",        defaultVisible: true  },
  { key: "assignee",      label: "Assignee",      defaultVisible: true  },
  { key: "is_escalated",  label: "Escalated",     defaultVisible: false },
  { key: "created_at",    label: "Created At",    defaultVisible: false },
  { key: "type",          label: "Type",          defaultVisible: false },
  { key: "actions",       label: "Actions",       defaultVisible: true  },
];

const COLUMNS_LS_KEY = "ticketlist.columns.v1";

// ─── Helpers ────────────────────────────────────────────────────────────────

function fmtDate(ts: string | null): string {
  if (!ts) return "—";
  try { return new Date(ts).toLocaleDateString([], { day: "2-digit", month: "short", year: "numeric" }); }
  catch { return ts; }
}

function fmtDateTime(ts: string | null): string {
  if (!ts) return "—";
  try {
    return new Date(ts).toLocaleString([], {
      day: "2-digit", month: "short", year: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  } catch { return ts; }
}

// A guest name must contain at least one letter and no digits. Allows Unicode
// letters (international guests), spaces, and . ' - (e.g. "Mary-Jane O'Neil",
// "José"). Returns an error string, or null when valid/empty (emptiness is
// handled by the required check).
function validateGuestName(raw: string): string | null {
  const value = (raw || "").trim();
  if (!value) return null;
  if (/\d/.test(value)) return "Name cannot contain numbers.";
  if (!/\p{L}/u.test(value)) return "Please enter a valid name.";
  return null;
}

function shortRange(from: string, to: string): string {
  // Render as "May 15 - May 22" style. If dates are missing, return empty string.
  if (!from && !to) return "";
  const fmt = (s: string) => {
    if (!s) return "";
    try {
      return new Date(s + "T00:00:00").toLocaleDateString([], { month: "short", day: "numeric" });
    } catch { return s; }
  };
  const left  = fmt(from);
  const right = fmt(to);
  if (left && right) return `${left} - ${right}`;
  return left || right;
}

function StatusBadge({ status }: { status: string }) {
  const label = status.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
  return (
    <span className={cn("px-2 py-0.5 rounded text-xs font-medium", STATUS_STYLES[status] ?? "bg-muted text-muted-foreground")}>
      {label}
    </span>
  );
}

function PriorityBadge({ priority }: { priority: string }) {
  return (
    <span className={cn("px-2 py-0.5 rounded text-xs font-medium capitalize", PRIORITY_STYLES[priority] ?? "bg-muted text-muted-foreground")}>
      {priority}
    </span>
  );
}

// ─── Single-select dropdown — used only inside dialogs (Create / Edit) ───────

function SelectField({
  options, value, onChange,
}: {
  options: Option[];
  value: string;
  onChange: (v: string) => void;
}) {
  const current = options.find(o => o.value === value);
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button className="mt-1 inline-flex items-center gap-1 px-2 py-1 text-[11px] rounded-md border bg-background hover:bg-muted/50 transition-colors w-full max-w-[140px]">
          <span className="truncate">{current?.label ?? "—"}</span>
          <ChevronDown className="h-3 w-3 opacity-60 ml-auto flex-shrink-0" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-[160px]">
        {options.map(o => (
          <DropdownMenuItem
            key={o.value}
            className={cn("text-xs cursor-pointer", value === o.value && "font-semibold text-primary")}
            onSelect={() => onChange(o.value)}
          >
            {o.label}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

// ─── Unified multi-select checkbox filter (column headers) ────────────────────
// value=[] means "All" (no filter). Clicking "All" row always clears selection.

function CheckboxFilter({
  options, value, onChange,
}: {
  options: Option[];
  value: string[];
  onChange: (next: string[]) => void;
}) {
  const [open,  setOpen]  = useState(false);
  const [draft, setDraft] = useState<string[]>(value);

  useEffect(() => { if (open) setDraft(value); }, [open, value]);

  const isAll = draft.length === 0;
  const label =
    isAll             ? "All"
    : draft.length === 1 ? (options.find(o => o.value === draft[0])?.label ?? draft[0])
    :                      `${draft.length} selected`;

  const toggleOne = (v: string, checked: boolean) => {
    setDraft(prev => {
      const set = new Set(prev.length === 0 ? options.map(o => o.value) : prev);
      if (checked) set.add(v); else set.delete(v);
      const arr = Array.from(set);
      return arr.length === options.length ? [] : arr;
    });
  };

  const isActive = value.length > 0;

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button className={cn(
          "mt-1 inline-flex items-center gap-1 px-2 py-1 text-[11px] rounded-md border bg-background",
          "hover:bg-muted/50 transition-colors w-full max-w-[140px]",
          isActive && "border-primary/50 text-primary font-semibold",
        )}>
          <span className="truncate">{label}</span>
          <ChevronDown className="h-3 w-3 opacity-60 ml-auto flex-shrink-0" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-48 p-2">
        <label className="flex items-center gap-2 px-2 py-1.5 text-xs cursor-pointer rounded hover:bg-muted/40">
          <Checkbox
            checked={isAll}
            onCheckedChange={() => setDraft(isAll ? options.map(o => o.value) : [])}
          />
          <span className="font-medium text-muted-foreground">All</span>
        </label>
        <div className="border-t my-1" />
        {options.map(o => {
          const checked = isAll || draft.includes(o.value);
          return (
            <label key={o.value} className="flex items-center gap-2 px-2 py-1.5 text-xs cursor-pointer rounded hover:bg-muted/40">
              <Checkbox checked={checked} onCheckedChange={(c) => toggleOne(o.value, Boolean(c))} />
              <span>{o.label}</span>
            </label>
          );
        })}
        <div className="flex justify-end gap-2 mt-2 pt-2 border-t">
          <Button size="sm" variant="outline" className="h-7 text-xs" onClick={() => setOpen(false)}>Cancel</Button>
          <Button size="sm" className="h-7 text-xs" onClick={() => { onChange(draft); setOpen(false); }}>Apply</Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}

// ─── Property column-header multi-select filter ──────────────────────────────

function PropertyHeaderFilter({
  options,
  value,
  onChange,
}: {
  options: string[];
  value: string[];
  onChange: (next: string[]) => void;
}) {
  const [open,  setOpen]  = useState(false);
  const [draft, setDraft] = useState<string[]>(value);

  useEffect(() => { if (open) setDraft(value); }, [open, value]);

  const allChecked = draft.length === 0;
  const label =
    allChecked           ? "All"
    : draft.length === 1 ? (draft[0].length > 14 ? draft[0].slice(0, 13) + "…" : draft[0])
    :                      `${draft.length} selected`;

  const toggleOne = (name: string, checked: boolean) => {
    setDraft(prev => {
      const set = new Set(prev.length === 0 ? options : prev);
      if (checked) set.add(name); else set.delete(name);
      const arr = Array.from(set);
      return arr.length === options.length ? [] : arr;
    });
  };

  if (options.length === 0) return null;
  const isActive = value.length > 0;

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button className={cn(
          "mt-1 inline-flex items-center gap-1 px-2 py-1 text-[11px] rounded-md border bg-background",
          "hover:bg-muted/50 transition-colors w-full max-w-[140px]",
          isActive && "border-primary/50 text-primary font-semibold",
        )}>
          <span className="truncate">{label}</span>
          <ChevronDown className="h-3 w-3 opacity-60 ml-auto flex-shrink-0" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-52 p-2">
        <div className="max-h-60 overflow-auto">
          <label className="flex items-center gap-2 px-2 py-1.5 text-xs cursor-pointer rounded hover:bg-muted/40">
            <Checkbox
              checked={allChecked}
              onCheckedChange={() => setDraft(allChecked ? options : [])}
            />
            <span className="font-medium text-muted-foreground">All</span>
          </label>
          <div className="border-t my-1" />
          {options.map(name => {
            const checked = allChecked || draft.includes(name);
            return (
              <label key={name} className="flex items-center gap-2 px-2 py-1.5 text-xs cursor-pointer rounded hover:bg-muted/40">
                <Checkbox checked={checked} onCheckedChange={(c) => toggleOne(name, Boolean(c))} />
                <span className="truncate">{name}</span>
              </label>
            );
          })}
        </div>
        <div className="flex justify-end gap-2 mt-2 pt-2 border-t">
          <Button size="sm" variant="outline" className="h-7 text-xs" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button size="sm" className="h-7 text-xs" onClick={() => { onChange(draft); setOpen(false); }}>
            Apply
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}

// ─── Columns(N) toggle popover ───────────────────────────────────────────────

function loadVisibleColumns(): Set<ColumnKey> {
  try {
    const raw = localStorage.getItem(COLUMNS_LS_KEY);
    if (raw) {
      const arr = JSON.parse(raw) as ColumnKey[];
      if (Array.isArray(arr) && arr.length > 0) return new Set(arr);
    }
  } catch { /* ignore */ }
  return new Set(COLUMN_DEFS.filter(c => c.defaultVisible).map(c => c.key));
}

function saveVisibleColumns(set: Set<ColumnKey>) {
  try { localStorage.setItem(COLUMNS_LS_KEY, JSON.stringify(Array.from(set))); } catch { /* ignore */ }
}

function ColumnsToggle({
  visible, onChange,
}: {
  visible: Set<ColumnKey>;
  onChange: (next: Set<ColumnKey>) => void;
}) {
  const toggle = (k: ColumnKey, checked: boolean) => {
    const next = new Set(visible);
    if (checked) next.add(k); else next.delete(k);
    onChange(next);
    saveVisibleColumns(next);
  };
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="outline" size="sm" className="gap-1.5 text-xs h-8">
          <Columns3 className="h-3.5 w-3.5" />
          Columns ({visible.size})
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-56 p-2">
        <div className="text-[11px] uppercase tracking-wide text-muted-foreground px-2 py-1">
          Toggle columns
        </div>
        <div className="max-h-72 overflow-auto">
          {COLUMN_DEFS.map(c => {
            const checked = visible.has(c.key);
            const isActions = c.key === "actions";
            return (
              <label
                key={c.key}
                className={cn(
                  "flex items-center gap-2 px-2 py-1.5 text-xs rounded",
                  isActions ? "opacity-50 cursor-not-allowed" : "cursor-pointer hover:bg-muted/40",
                )}
              >
                <Checkbox
                  checked={checked}
                  disabled={isActions}
                  onCheckedChange={(v) => !isActions && toggle(c.key, Boolean(v))}
                />
                <span>{c.label}</span>
              </label>
            );
          })}
        </div>
      </PopoverContent>
    </Popover>
  );
}

// ─── Stat Card ───────────────────────────────────────────────────────────────

interface StatCardProps {
  label: string;
  count: number;
  icon: React.ReactNode;
  colorClass: string;
  subtitle?: string;
  onClick?: () => void;
  active?: boolean;
}

function StatCard({ label, count, icon, colorClass, subtitle, onClick, active }: StatCardProps) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex flex-col gap-1.5 p-4 rounded-lg border text-left transition-all",
        "hover:shadow-sm hover:border-primary/30",
        active ? "border-primary/40 bg-primary/5 shadow-sm" : "bg-card",
      )}
    >
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-muted-foreground">{label}</span>
        <span className={cn("p-1.5 rounded-md", colorClass)}>{icon}</span>
      </div>
      <span className="text-2xl font-bold tabular-nums">{count.toLocaleString()}</span>
      {subtitle ? (
        <span className="text-[11px] text-muted-foreground/70">{subtitle}</span>
      ) : null}
    </button>
  );
}

// ─── Create Ticket Dialog (manual staff creation) ────────────────────────────

function CreateDialog({
  onClose, onCreated, properties,
}: {
  onClose:   () => void;
  onCreated: () => void;
  /** The active hotel's real sub-property names (from /api/hotel-properties). When
   *  non-empty, the Property field is a dropdown locked to these values so staff
   *  can't type an arbitrary place (e.g. "Bangalore"). Empty → free-text fallback
   *  for single-property hotels with no sub-property rows. */
  properties: string[];
}) {
  const [guestName,    setGuestName]    = useState("");
  const [description,  setDescription]  = useState("");
  const [propertyName, setPropertyName] = useState("");
  const [roomNumber,   setRoomNumber]   = useState("");
  const [phase,        setPhase]        = useState("booking");
  const [priority,     setPriority]     = useState("medium");
  const [department,   setDepartment]   = useState("Reception Front Office");
  const [status,       setStatus]       = useState("open");
  const [assignee,     setAssignee]     = useState("");
  const [saving,       setSaving]       = useState(false);
  const [error,        setError]        = useState<string | null>(null);

  const nameError = validateGuestName(guestName);
  const canSave = guestName.trim() && description.trim() && !nameError;

  const save = async () => {
    if (!canSave) return;
    setSaving(true);
    setError(null);
    try {
      await adminSend("POST", "/tickets", {
        guest_name:    guestName.trim(),
        description:   description.trim(),
        property_name: propertyName.trim() || null,
        room_number:   roomNumber.trim() || null,
        phase, priority, department, status,
        assignee:      assignee.trim() || null,
      });
      onCreated();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create ticket");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open onOpenChange={onClose}>
      <DialogContent className="max-w-md" aria-describedby={undefined}>
        <DialogHeader>
          <DialogTitle className="text-sm font-semibold">Create Ticket</DialogTitle>
        </DialogHeader>
        <div className="space-y-3 py-2">
          <div>
            <label className="block text-xs font-medium text-muted-foreground mb-1">Guest Name *</label>
            <Input className="h-8 text-xs" value={guestName} onChange={e => setGuestName(e.target.value)} placeholder="John Doe" />
            {nameError && guestName.trim() ? <p className="text-[11px] text-red-600 mt-1">{nameError}</p> : null}
          </div>
          <div>
            <label className="block text-xs font-medium text-muted-foreground mb-1">Description *</label>
            <textarea
              className="w-full h-20 text-xs border rounded-md px-2.5 py-2 resize-none bg-background focus:outline-none focus:ring-1 focus:ring-primary"
              value={description} onChange={e => setDescription(e.target.value)}
              placeholder="Short description of the request"
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-muted-foreground mb-1">Property</label>
              {properties.length > 0 ? (
                <SelectField
                  options={[{ value: "", label: "— Select —" }, ...properties.map(n => ({ value: n, label: n }))]}
                  value={propertyName}
                  onChange={setPropertyName}
                />
              ) : (
                <Input className="h-8 text-xs" value={propertyName} onChange={e => setPropertyName(e.target.value)} placeholder="e.g. Orchid Passaros Goa" />
              )}
            </div>
            <div>
              <label className="block text-xs font-medium text-muted-foreground mb-1">Room No</label>
              <Input className="h-8 text-xs" value={roomNumber} onChange={e => setRoomNumber(e.target.value)} placeholder="—" />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-muted-foreground mb-1">Stage</label>
              <SelectField options={PHASES.slice(1)} value={phase} onChange={setPhase} />
            </div>
            <div>
              <label className="block text-xs font-medium text-muted-foreground mb-1">Priority</label>
              <SelectField options={PRIORITIES.slice(1)} value={priority} onChange={setPriority} />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-muted-foreground mb-1">Department</label>
              <SelectField options={DEPARTMENTS.slice(1)} value={department} onChange={setDepartment} />
            </div>
            <div>
              <label className="block text-xs font-medium text-muted-foreground mb-1">Status</label>
              <SelectField options={EDIT_STATUSES} value={status} onChange={setStatus} />
            </div>
          </div>
          <div>
            <label className="block text-xs font-medium text-muted-foreground mb-1">Assignee</label>
            <Input className="h-8 text-xs" value={assignee} onChange={e => setAssignee(e.target.value)} placeholder="Name or email" />
          </div>
          {error && <p className="text-xs text-red-600">{error}</p>}
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={onClose}>Cancel</Button>
          <Button size="sm" onClick={save} disabled={saving || !canSave}>
            {saving ? "Creating…" : "Create"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ─── Edit Ticket Dialog ──────────────────────────────────────────────────────

function EditDialog({
  ticket, onClose, onSaved,
}: {
  ticket: TicketRow;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [status,      setStatus]      = useState(ticket.status);
  const [priority,    setPriority]    = useState(ticket.priority);
  const [assignee,    setAssignee]    = useState(ticket.assignee ?? "");
  const [dept,        setDept]        = useState(ticket.department ?? "");
  const [description, setDescription] = useState(ticket.description ?? "");
  const [notes,       setNotes]       = useState(ticket.manager_notes ?? "");
  const [escalated,   setEscalated]   = useState(ticket.is_escalated);
  const [saving,      setSaving]      = useState(false);

  const save = async () => {
    setSaving(true);
    try {
      await adminSend("PUT", `/tickets/${ticket.ticket_db_id}`, {
        status, priority,
        assignee:     assignee || null,
        department:   dept || null,
        description:  description || null,
        manager_notes: notes || null,
        is_escalated: escalated,
      });
      onSaved();
    } catch { /* TODO: toast */ }
    finally { setSaving(false); }
  };

  return (
    <Dialog open onOpenChange={onClose}>
      <DialogContent className="max-w-md" aria-describedby={undefined}>
        <DialogHeader>
          <DialogTitle className="text-sm font-semibold">
            Update Ticket · <span className="font-mono text-muted-foreground">{ticket.ticket_ref}</span>
          </DialogTitle>
        </DialogHeader>
        <div className="space-y-3 py-2">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-muted-foreground mb-1">Status</label>
              <SelectField options={EDIT_STATUSES} value={status} onChange={setStatus} />
            </div>
            <div>
              <label className="block text-xs font-medium text-muted-foreground mb-1">Priority</label>
              <SelectField options={PRIORITIES.slice(1)} value={priority} onChange={setPriority} />
            </div>
          </div>
          <div>
            <label className="block text-xs font-medium text-muted-foreground mb-1">Assignee</label>
            <Input className="h-8 text-xs" value={assignee} onChange={e => setAssignee(e.target.value)} placeholder="Name or email" />
          </div>
          <div>
            <label className="block text-xs font-medium text-muted-foreground mb-1">Department</label>
            <SelectField options={DEPARTMENTS.slice(1)} value={dept} onChange={setDept} />
          </div>
          <div>
            <label className="block text-xs font-medium text-muted-foreground mb-1">Description</label>
            <textarea
              className="w-full h-16 text-xs border rounded-md px-2.5 py-2 resize-none bg-background focus:outline-none focus:ring-1 focus:ring-primary"
              value={description} onChange={e => setDescription(e.target.value)}
              placeholder="Short description for staff"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-muted-foreground mb-1">Manager Notes</label>
            <textarea
              className="w-full h-20 text-xs border rounded-md px-2.5 py-2 resize-none bg-background focus:outline-none focus:ring-1 focus:ring-primary"
              value={notes} onChange={e => setNotes(e.target.value)}
              placeholder="Internal notes…"
            />
          </div>
          <label className="flex items-center gap-2 text-xs cursor-pointer select-none">
            <input type="checkbox" checked={escalated} onChange={e => setEscalated(e.target.checked)}
              className="accent-primary" />
            Mark as Escalated
          </label>
        </div>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={onClose}>Cancel</Button>
          <Button size="sm" onClick={save} disabled={saving}>
            {saving ? "Saving…" : "Save Changes"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ─── Detail Dialog ───────────────────────────────────────────────────────────

interface ConvMessage { role: string; message: string; timestamp?: string; }

function DetailDialog({ ticket, onClose }: { ticket: TicketRow; onClose: () => void }) {
  const [convMsgs, setConvMsgs] = useState<ConvMessage[]>([]);
  const [convLoading, setConvLoading] = useState(false);

  useEffect(() => {
    if (!ticket.session_id) return;
    setConvLoading(true);
    // Scope the transcript to the ONE chat thread that raised this ticket.
    // conversation_uid is stamped on the ticket at creation; when present we
    // pass it so a session with many conversations only shows the relevant one.
    // Legacy tickets (no uid) fall back to the full-session transcript.
    const uid = (ticket.conversation_uid || "").trim();
    const qs = uid ? `?conversation_uid=${encodeURIComponent(uid)}` : "";
    adminGet<{ messages: ConvMessage[] }>(`/engage-sessions/${encodeURIComponent(ticket.session_id)}${qs}`)
      .then(d => setConvMsgs(d.messages ?? []))
      .catch(() => setConvMsgs([]))
      .finally(() => setConvLoading(false));
  }, [ticket.session_id, ticket.conversation_uid]);

  const rows: [string, string | null | boolean][] = [
    ["Ticket ID",    ticket.ticket_ref],
    ["Guest",        ticket.guest_name],
    ["Room",         ticket.room_number],
    ["Property",     ticket.property_name],
    ["Description",  ticket.description],
    ["Session",      ticket.session_id],
    ["Issue",        ticket.issue],
    ["Category",     ticket.category],
    ["Sub-category", ticket.sub_category],
    ["Phase/Stage",  ticket.phase ? (PHASE_LABELS[ticket.phase] ?? ticket.phase) : null],
    ["Priority",     ticket.priority],
    ["Status",       ticket.status],
    ["Department",   ticket.department],
    ["Assignee",     ticket.assignee],
    ["Escalated",    ticket.is_escalated ? "Yes" : "No"],
    ["SLA Due",      fmtDateTime(ticket.sla_due_at)],
    ["Closed At",    fmtDateTime(ticket.closed_at)],
    ["Created At",   fmtDateTime(ticket.created_at)],
    ["Notes",        ticket.manager_notes],
    ["Sentiment",    ticket.sentiment_score],
    ["Source",       ticket.source],
  ];
  return (
    <Dialog open onOpenChange={onClose}>
      <DialogContent className="max-w-lg max-h-[85vh] overflow-y-auto" aria-describedby={undefined}>
        <DialogHeader>
          <DialogTitle className="text-sm font-semibold">
            Ticket Detail · <span className="font-mono text-muted-foreground">{ticket.ticket_ref}</span>
          </DialogTitle>
        </DialogHeader>
        <div className="divide-y text-xs">
          {rows.filter(([, v]) => v !== null && v !== "" && v !== undefined).map(([label, val]) => (
            <div key={label} className="flex gap-3 py-2">
              <span className="w-28 flex-shrink-0 text-muted-foreground font-medium">{label}</span>
              <span className="flex-1 break-words">{String(val)}</span>
            </div>
          ))}
        </div>

        {ticket.session_id && (
          <div className="mt-4">
            <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground mb-2">
              Conversation History
            </p>
            {convLoading ? (
              <p className="text-xs text-muted-foreground">Loading…</p>
            ) : convMsgs.length === 0 ? (
              <p className="text-xs text-muted-foreground">No messages found.</p>
            ) : (
              <div className="flex flex-col gap-2">
                {convMsgs.map((m, i) => (
                  <div
                    key={i}
                    className={cn(
                      "text-xs rounded-md px-3 py-2 max-w-[90%]",
                      m.role === "user"
                        ? "bg-primary/10 self-end text-right"
                        : "bg-muted self-start",
                    )}
                  >
                    <span className="font-medium text-[10px] uppercase tracking-wide text-muted-foreground block mb-0.5">
                      {m.role === "user" ? "Guest" : "Bot"}
                    </span>
                    {m.message}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" size="sm" onClick={onClose}>Close</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ─── Main Page ───────────────────────────────────────────────────────────────

interface PropertyOption { code: string; name: string; }

export default function TicketList() {
  const [tickets,      setTickets]      = useState<TicketRow[]>([]);
  const [stats,        setStats]        = useState<Stats>({ total: 0, open: 0, in_progress: 0, closed: 0, escalated: 0, cancelled: 0 });
  const [loading,      setLoading]      = useState(true);
  const [search,              setSearch]              = useState("");
  const [debSearch,           setDebSearch]           = useState("");
  const [dateFrom,            setDateFrom]            = useState("");
  const [dateTo,              setDateTo]              = useState("");
  const [phase,               setPhase]               = useState("all");
  const [priority,            setPriority]            = useState("all");
  const [selectedDepartments, setSelectedDepartments] = useState<string[]>([]);
  const [selectedStatuses,    setSelectedStatuses]    = useState<string[]>([]);
  const [editTicket,   setEditTicket]   = useState<TicketRow | null>(null);
  const [detailTicket, setDetailTicket] = useState<TicketRow | null>(null);
  const [createOpen,   setCreateOpen]   = useState(false);
  const [lastUpdated,  setLastUpdated]  = useState<Date | null>(null);
  const [visibleCols,  setVisibleCols]  = useState<Set<ColumnKey>>(() => loadVisibleColumns());

  // Property selector
  const [propertyCode,       setPropertyCode]       = useState<string>(getActivePropertyCode());
  const [propertyOptions,    setPropertyOptions]    = useState<PropertyOption[]>([]);
  const [subPropertyNames,   setSubPropertyNames]   = useState<string[]>([]);
  const [selectedProperties, setSelectedProperties] = useState<string[]>([]);

  // Load hotel-level options (for the top-right hotel switcher)
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
      .catch(() => {/* ignore */});
  }, []);

  // Load sub-properties for the column filter (re-fetch when active hotel changes)
  useEffect(() => {
    adminGet<{ properties?: Array<{ name?: string }> }>("/hotel-properties", propertyCode)
      .then(payload => {
        const names = (payload?.properties ?? [])
          .map(p => String(p?.name || "").trim())
          .filter(Boolean)
          .sort();
        setSubPropertyNames(names);
        setSelectedProperties([]);
      })
      .catch(() => setSubPropertyNames([]));
  }, [propertyCode]);

  // Debounce search
  useEffect(() => {
    const t = setTimeout(() => setDebSearch(search), 350);
    return () => clearTimeout(t);
  }, [search]);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const p = new URLSearchParams({ limit: "500", offset: "0" });
      if (debSearch)      p.set("search", debSearch);
      if (dateFrom)       p.set("date_from", dateFrom);
      if (dateTo)         p.set("date_to", dateTo);
      if (phase !== "all")     p.set("phase", phase);
      if (priority !== "all")  p.set("priority", priority);

      const statsParams = new URLSearchParams();
      if (dateFrom) statsParams.set("date_from", dateFrom);
      if (dateTo)   statsParams.set("date_to",   dateTo);

      const [tkts, st] = await Promise.all([
        adminGet<{ tickets: TicketRow[] }>(`/tickets?${p}`, propertyCode),
        adminGet<Stats>(`/tickets/stats${statsParams.toString() ? `?${statsParams}` : ""}`, propertyCode),
      ]);
      setTickets(tkts.tickets ?? []);
      setStats(st);
      setLastUpdated(new Date());
    } catch {
      setTickets([]);
    } finally {
      setLoading(false);
    }
  }, [debSearch, dateFrom, dateTo, phase, priority, propertyCode]);

  useEffect(() => { fetchAll(); }, [fetchAll]);

  const hasFilters =
    !!debSearch || !!dateFrom || !!dateTo ||
    phase !== "all" || priority !== "all" ||
    selectedDepartments.length > 0 || selectedStatuses.length > 0 || selectedProperties.length > 0;

  const filteredTickets = useMemo(() => {
    let result = tickets;
    if (selectedProperties.length > 0)
      result = result.filter(t => t.property_name != null && selectedProperties.includes(t.property_name));
    if (selectedDepartments.length > 0)
      result = result.filter(t => t.department != null && selectedDepartments.includes(t.department));
    if (selectedStatuses.length > 0)
      result = result.filter(t => selectedStatuses.includes(t.status));
    return result;
  }, [tickets, selectedProperties, selectedDepartments, selectedStatuses]);

  const dateSubtitle = useMemo(() => shortRange(dateFrom, dateTo), [dateFrom, dateTo]);

  const isVisible = (k: ColumnKey) => visibleCols.has(k);

  const clearFilters = () => {
    setSearch(""); setDateFrom(""); setDateTo("");
    setPhase("all"); setPriority("all");
    setSelectedDepartments([]); setSelectedStatuses([]); setSelectedProperties([]);
  };

  return (
    <div className="flex-1 overflow-auto min-h-0">

      {/* ── Header ── */}
      <div className="flex items-start justify-between px-6 py-5 border-b">
        <div>
          <h1 className="text-xl font-bold tracking-tight">Ticket List for Guests</h1>
          <p className="text-sm text-muted-foreground mt-0.5">View and manage guest-related tickets</p>
        </div>
        <div className="flex items-center gap-2">
          {/* Property selector */}
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
            onClick={fetchAll}
            disabled={loading}
            className="gap-1.5 text-xs"
          >
            <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
            Refresh
          </Button>
        </div>
      </div>

      {/* ── Stats Cards ── */}
      <div className="grid grid-cols-6 gap-3 px-6 py-4 border-b bg-muted/10">
        <StatCard label="Total Tickets" count={stats.total}       icon={<Ticket        className="h-3.5 w-3.5" />} colorClass="bg-muted/60          text-muted-foreground"  subtitle={dateSubtitle} />
        <StatCard label="Open Tickets"  count={stats.open}        icon={<Clock         className="h-3.5 w-3.5" />} colorClass="bg-primary/15        text-primary"           subtitle={dateSubtitle} />
        <StatCard label="In Progress"   count={stats.in_progress} icon={<TrendingUp    className="h-3.5 w-3.5" />} colorClass="bg-primary/10        text-primary/80"        subtitle={dateSubtitle} />
        <StatCard label="Closed Tickets"count={stats.closed}      icon={<CheckCircle2  className="h-3.5 w-3.5" />} colorClass="bg-muted             text-foreground/70"     subtitle={dateSubtitle} />
        <StatCard label="Escalated"     count={stats.escalated}   icon={<AlertTriangle className="h-3.5 w-3.5" />} colorClass="bg-destructive/15    text-destructive"       subtitle={dateSubtitle} />
        <StatCard label="Cancelled"     count={stats.cancelled}   icon={<XCircle       className="h-3.5 w-3.5" />} colorClass="bg-muted/40          text-muted-foreground/70" subtitle={dateSubtitle} />
      </div>

      {/* ── Top filter bar (search + dates + columns + create) ── */}
      <div className="flex flex-wrap items-center gap-2 px-6 py-3 border-b bg-background">
        <div className="relative">
          <Search className="absolute left-2.5 top-2 h-3.5 w-3.5 text-muted-foreground" />
          <Input
            placeholder="Search Guest, Description, Assignee"
            className="pl-8 h-8 text-xs w-72"
            value={search}
            onChange={e => setSearch(e.target.value)}
          />
        </div>

        <div className="flex items-center gap-2 border rounded-md px-3 py-1.5 bg-muted/20">
          <span className="text-[11px] font-medium text-muted-foreground whitespace-nowrap">From</span>
          <input
            type="date"
            className="text-xs bg-transparent border-none outline-none text-foreground w-28 cursor-pointer"
            value={dateFrom}
            onChange={e => setDateFrom(e.target.value)}
          />
          <span className="text-muted-foreground/60">→</span>
          <input
            type="date"
            className="text-xs bg-transparent border-none outline-none text-foreground w-28 cursor-pointer"
            value={dateTo}
            onChange={e => setDateTo(e.target.value)}
          />
          {(dateFrom || dateTo) && (
            <button
              className="text-[11px] text-muted-foreground hover:text-foreground ml-1"
              onClick={() => { setDateFrom(""); setDateTo(""); }}
            >✕</button>
          )}
        </div>

        {hasFilters && (
          <Button variant="ghost" size="sm" className="h-8 text-xs px-2 text-muted-foreground" onClick={clearFilters}>
            Clear filters
          </Button>
        )}

        <div className="ml-auto flex items-center gap-2">
          {lastUpdated && (
            <span className="text-[11px] text-muted-foreground/60">
              {filteredTickets.length} tickets · updated {(() => {
                const s = Math.floor((Date.now() - lastUpdated.getTime()) / 1000);
                return s < 60 ? "just now" : `${Math.floor(s/60)}m ago`;
              })()}
            </span>
          )}
          <ColumnsToggle visible={visibleCols} onChange={setVisibleCols} />
          <Button size="sm" className="gap-1.5 text-xs h-8" onClick={() => setCreateOpen(true)}>
            <Plus className="h-3.5 w-3.5" />
            Create Ticket
          </Button>
        </div>
      </div>

      {/* ── Table — header always visible (filters work even with no rows) ── */}
      <div className="overflow-x-auto">
        <table className="w-full text-xs border-collapse">
          <thead className="bg-muted/60">
            <tr className="border-b">
              {COLUMN_DEFS.filter(c => isVisible(c.key)).map(c => (
                <th key={c.key} className="text-left px-2 py-2 font-semibold text-muted-foreground align-top text-[11px] uppercase tracking-wide border-b">
                  <div className="flex flex-col gap-1">
                    <span className="whitespace-nowrap">{c.label}</span>
                    {c.key === "phase"         && <SelectField options={PHASES}     value={phase}    onChange={setPhase}    />}
                    {c.key === "priority"      && <SelectField options={PRIORITIES} value={priority} onChange={setPriority} />}
                    {c.key === "department"    && <CheckboxFilter options={DEPARTMENTS.slice(1)} value={selectedDepartments} onChange={setSelectedDepartments} />}
                    {c.key === "status"        && <CheckboxFilter options={STATUS_OPTIONS}        value={selectedStatuses}    onChange={setSelectedStatuses}    />}
                    {c.key === "property_name" && <PropertyHeaderFilter options={subPropertyNames} value={selectedProperties} onChange={setSelectedProperties} />}
                  </div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={COLUMN_DEFS.filter(c => isVisible(c.key)).length} className="text-center py-12">
                  <div className="flex items-center justify-center text-muted-foreground text-sm gap-2">
                    <RefreshCw className="h-4 w-4 animate-spin" /> Loading tickets…
                  </div>
                </td>
              </tr>
            ) : filteredTickets.length === 0 ? (
              <tr>
                <td colSpan={COLUMN_DEFS.filter(c => isVisible(c.key)).length} className="text-center py-12">
                  <div className="flex flex-col items-center justify-center gap-3 text-muted-foreground">
                    <Ticket className="h-10 w-10 opacity-15" />
                    <p className="text-sm font-medium text-foreground/50">No data available</p>
                    <p className="text-xs opacity-50">{hasFilters ? "Try clearing filters" : "Tickets raised by the bot will appear here"}</p>
                  </div>
                </td>
              </tr>
            ) : (
              filteredTickets.map((t, i) => (
                <tr
                  key={t.ticket_db_id}
                  className={cn(
                    "border-b transition-colors hover:bg-muted/30 cursor-default",
                    i % 2 === 0 ? "bg-background" : "bg-muted/10",
                  )}
                >
                  {isVisible("ticket_ref") && (
                    <td className="px-2 py-2 font-mono text-[11px] text-muted-foreground whitespace-nowrap">
                      {t.ticket_ref || <span className="text-muted-foreground/40">—</span>}
                    </td>
                  )}
                  {isVisible("room_number") && (
                    <td className="px-2 py-2 whitespace-nowrap">
                      {t.room_number ?? <span className="text-muted-foreground/40">—</span>}
                    </td>
                  )}
                  {isVisible("property_name") && (
                    <td className="px-2 py-2 max-w-[120px]" title={t.property_name ?? ""}>
                      {t.property_name
                        ? <span className="block truncate">{t.property_name}</span>
                        : <span className="text-muted-foreground/40">—</span>
                      }
                    </td>
                  )}
                  {isVisible("guest_name") && (
                    <td className="px-2 py-2 font-medium max-w-[120px]" title={t.guest_name}>
                      <span className="block truncate">{t.guest_name}</span>
                    </td>
                  )}
                  {isVisible("description") && (
                    <td className="px-2 py-2 max-w-[160px]">
                      <span className="line-clamp-2 leading-snug" title={t.description ?? t.issue}>
                        {t.description ?? (
                          t.issue
                            ? <span className="text-muted-foreground/70 italic">{t.issue}</span>
                            : <span className="text-muted-foreground/40">—</span>
                        )}
                      </span>
                    </td>
                  )}
                  {isVisible("phase") && (
                    <td className="px-2 py-2 whitespace-nowrap">
                      {t.phase
                        ? <span className="text-primary bg-primary/10 border border-primary/20 px-1.5 py-0.5 rounded text-[11px] font-medium">
                            {PHASE_LABELS[t.phase] ?? t.phase}
                          </span>
                        : <span className="text-muted-foreground/40">—</span>
                      }
                    </td>
                  )}
                  {isVisible("priority") && (
                    <td className="px-2 py-2 whitespace-nowrap">
                      <PriorityBadge priority={t.priority} />
                    </td>
                  )}
                  {isVisible("department") && (
                    <td className="px-2 py-2 whitespace-nowrap max-w-[110px] truncate" title={t.department ?? ""}>
                      {t.department ?? <span className="text-muted-foreground/40">—</span>}
                    </td>
                  )}
                  {isVisible("status") && (
                    <td className="px-2 py-2 whitespace-nowrap">
                      <StatusBadge status={t.status} />
                    </td>
                  )}
                  {isVisible("assignee") && (
                    <td className="px-2 py-2 whitespace-nowrap max-w-[80px] truncate" title={t.assignee ?? ""}>
                      {t.assignee ?? <span className="text-muted-foreground/40">—</span>}
                    </td>
                  )}
                  {isVisible("is_escalated") && (
                    <td className="px-2 py-2 whitespace-nowrap">
                      {t.is_escalated
                        ? <span className="text-red-600 font-semibold">Yes</span>
                        : <span className="text-muted-foreground/40 text-[11px]">No</span>
                      }
                    </td>
                  )}
                  {isVisible("created_at") && (
                    <td className="px-2 py-2 whitespace-nowrap text-muted-foreground">
                      {fmtDate(t.created_at)}
                    </td>
                  )}
                  {isVisible("type") && (
                    <td className="px-2 py-2 whitespace-nowrap">
                      <span className="capitalize">{t.sub_category ?? t.category}</span>
                    </td>
                  )}
                  {isVisible("actions") && (
                    <td className="px-2 py-2 whitespace-nowrap">
                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <button className="p-1 rounded hover:bg-muted/60 transition-colors">
                            <MoreHorizontal className="h-4 w-4 text-muted-foreground" />
                          </button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end" className="w-36">
                          <DropdownMenuItem className="gap-2 text-xs cursor-pointer" onSelect={() => setDetailTicket(t)}>
                            <Eye className="h-3.5 w-3.5" /> View Details
                          </DropdownMenuItem>
                          <DropdownMenuItem className="gap-2 text-xs cursor-pointer" onSelect={() => setEditTicket(t)}>
                            <Pencil className="h-3.5 w-3.5" /> Update Ticket
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </td>
                  )}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* ── Dialogs ── */}
      {editTicket && (
        <EditDialog
          ticket={editTicket}
          onClose={() => setEditTicket(null)}
          onSaved={() => { setEditTicket(null); fetchAll(); }}
        />
      )}
      {detailTicket && (
        <DetailDialog ticket={detailTicket} onClose={() => setDetailTicket(null)} />
      )}
      {createOpen && (
        <CreateDialog
          properties={subPropertyNames}
          onClose={() => setCreateOpen(false)}
          onCreated={() => { setCreateOpen(false); fetchAll(); }}
        />
      )}
    </div>
  );
}
