import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { PHONE_COUNTRIES, phoneCountry, type PhoneCountryCode } from "@/lib/phone";

interface PhoneCountryPickerProps {
  value: PhoneCountryCode;
  onChange: (country: PhoneCountryCode) => void;
  /** Classes/style for the trigger button, so each form keeps its own look. */
  className?: string;
  style?: CSSProperties;
  accentColor?: string;
}

// Searchable country-code picker for form phone fields. Plain React (no Radix
// portal) so it works the same inside the embedded widget iframe.
export function PhoneCountryPicker({ value, onChange, className, style, accentColor }: PhoneCountryPickerProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const selected = phoneCountry(value);

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return PHONE_COUNTRIES;
    const dialQuery = q.replace(/^\+/, "");
    const rank = (c: (typeof PHONE_COUNTRIES)[number]) => {
      const name = c.name.toLowerCase();
      if (c.iso.toLowerCase() === q || c.dial.slice(1) === dialQuery) return 0;
      if (name.startsWith(q)) return 1;
      if (name.includes(q)) return 2;
      if (/^\d+$/.test(dialQuery) && c.dial.slice(1).startsWith(dialQuery)) return 3;
      return -1;
    };
    return PHONE_COUNTRIES.map((c) => ({ c, r: rank(c) }))
      .filter((m) => m.r >= 0)
      .sort((a, b) => a.r - b.r)
      .map((m) => m.c);
  }, [query]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent | TouchEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("touchstart", onPointerDown);
    panelRef.current?.scrollIntoView?.({ block: "nearest" });
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("touchstart", onPointerDown);
    };
  }, [open]);

  const pick = (country: PhoneCountryCode) => {
    onChange(country);
    setOpen(false);
    setQuery("");
  };

  return (
    <div ref={rootRef} className="relative shrink-0">
      <button
        type="button"
        aria-label="Country code"
        aria-haspopup="listbox"
        aria-expanded={open}
        className={className}
        style={style}
        onClick={() => setOpen((o) => !o)}
      >
        <span aria-hidden="true">{selected.flag}</span>
        <span>{selected.dial}</span>
      </button>
      {open ? (
        <div
          ref={panelRef}
          className="absolute left-0 top-full z-50 mt-1 w-64 max-w-[80vw] overflow-hidden rounded-lg border border-slate-200 bg-white text-slate-800 shadow-lg"
        >
          <input
            autoFocus
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              // Enter must pick, not submit the surrounding form.
              if (event.key === "Enter") {
                event.preventDefault();
                if (matches[0]) pick(matches[0].iso);
              } else if (event.key === "Escape") {
                event.preventDefault();
                setOpen(false);
              }
            }}
            placeholder="Search country or code"
            className="w-full border-b border-slate-100 px-3 py-2 text-sm outline-none"
          />
          <ul role="listbox" aria-label="Countries" className="max-h-56 overflow-y-auto py-1 text-sm">
            {matches.length === 0 ? (
              <li className="px-3 py-2 text-slate-400">No match</li>
            ) : (
              matches.map((c) => (
                <li key={c.iso} role="option" aria-selected={c.iso === value}>
                  <button
                    type="button"
                    onClick={() => pick(c.iso)}
                    className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-slate-100"
                    style={c.iso === value ? { color: accentColor, fontWeight: 600 } : undefined}
                  >
                    <span aria-hidden="true">{c.flag}</span>
                    <span className="flex-1 truncate">{c.name}</span>
                    <span className="text-slate-400">{c.dial}</span>
                  </button>
                </li>
              ))
            )}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
