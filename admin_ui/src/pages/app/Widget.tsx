import { useEffect, useMemo, useRef, useState } from "react";
import { componentRegistry } from "@/lib/componentRegistry";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useToast } from "@/hooks/use-toast";
import { UNPINNED_MODEL, type ModelOption } from "@/lib/widgetModel";
import { Copy, Plus, RefreshCw, Trash2, ExternalLink, Eye, Upload } from "lucide-react";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface Property {
  code: string;
  name: string;
  city: string;
}

interface Theme {
  brand_color: string;
  accent_color: string;
  bg_color: string;
  text_color: string;
}

interface Size {
  width: number;
  height: number;
}

interface Deployment {
  widget_key: string;
  hotel_code: string;
  name: string;
  status: "active" | "inactive";
  allowed_origins: string[];
  theme: Theme;
  size: Size;
  position: "left" | "right";
  bot_name: string;
  phase: string;
  auto_open: boolean;
  logo_url: string;
  session_ttl_hours: number;
  idle_timeout_minutes: number;
  /**
   * Pinned chat model for this embed. "" = unpinned (follows the /chat harness
   * dropdown / env default). When set, every guest turn on this embed uses this
   * model and its prompt variant, and the harness dropdown cannot move it.
   */
  model: string;
  created_at?: string;
  updated_at?: string;
}


const DEFAULT_THEME: Theme = {
  brand_color: "#FF6816",
  accent_color: "#FF6816",
  bg_color: "#FFFFFF",
  text_color: "#1A1A2E",
};

const DEFAULT_SIZE: Size = { width: 380, height: 620 };

// ---------------------------------------------------------------------------
// Snippet generators
// ---------------------------------------------------------------------------

function loaderSrc(): string {
  return `${window.location.origin}/static/embed/kebo-widget-loader.js`;
}

function htmlSnippet(d: Deployment): string {
  return `<!-- Kebo chat widget -->
<script
  src="${loaderSrc()}"
  data-widget-key="${d.widget_key}"
  async
></script>`;
}

function reactSnippet(d: Deployment): string {
  return `import { useEffect } from "react";

export function KeboWidget() {
  useEffect(() => {
    const s = document.createElement("script");
    s.src = "${loaderSrc()}";
    s.dataset.widgetKey = "${d.widget_key}";
    s.async = true;
    document.body.appendChild(s);
    return () => {
      s.remove();
      (window as any).KeboWidget?.destroy?.();
    };
  }, []);
  return null;
}`;
}

function nextSnippet(d: Deployment): string {
  return `import Script from "next/script";

export default function KeboWidget() {
  return (
    <Script
      src="${loaderSrc()}"
      data-widget-key="${d.widget_key}"
      strategy="afterInteractive"
    />
  );
}`;
}

function vueSnippet(d: Deployment): string {
  return `<script setup lang="ts">
import { onMounted, onBeforeUnmount } from "vue";

let scriptEl: HTMLScriptElement | null = null;
onMounted(() => {
  scriptEl = document.createElement("script");
  scriptEl.src = "${loaderSrc()}";
  scriptEl.dataset.widgetKey = "${d.widget_key}";
  scriptEl.async = true;
  document.body.appendChild(scriptEl);
});
onBeforeUnmount(() => {
  scriptEl?.remove();
  (window as any).KeboWidget?.destroy?.();
});
</script>`;
}

function angularSnippet(d: Deployment): string {
  return `import { Component, OnDestroy, OnInit } from "@angular/core";

@Component({
  selector: "kebo-widget",
  standalone: true,
  template: "",
})
export class KeboWidgetComponent implements OnInit, OnDestroy {
  private scriptEl: HTMLScriptElement | null = null;

  ngOnInit(): void {
    this.scriptEl = document.createElement("script");
    this.scriptEl.src = "${loaderSrc()}";
    this.scriptEl.dataset["widgetKey"] = "${d.widget_key}";
    this.scriptEl.async = true;
    document.body.appendChild(this.scriptEl);
  }

  ngOnDestroy(): void {
    this.scriptEl?.remove();
    (window as any).KeboWidget?.destroy?.();
  }
}`;
}

function wordpressSnippet(d: Deployment): string {
  return `<!-- Paste in Appearance > Theme File Editor > footer.php, just before </body> -->
<script
  src="${loaderSrc()}"
  data-widget-key="${d.widget_key}"
  async
></script>`;
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

const emptyForm: Omit<Deployment, "widget_key" | "created_at" | "updated_at"> = {
  hotel_code: "",
  name: "",
  status: "active",
  allowed_origins: [],
  theme: { ...DEFAULT_THEME },
  size: { ...DEFAULT_SIZE },
  position: "right",
  bot_name: "Assistant",
  phase: "pre_booking",
  auto_open: false,
  logo_url: "",
  session_ttl_hours: 24,
  idle_timeout_minutes: 30,
  model: "",
};

export default function Widget() {
  const { toast } = useToast();
  const [properties, setProperties] = useState<Property[]>([]);
  const [selectedHotel, setSelectedHotel] = useState<string>("");
  const [deployments, setDeployments] = useState<Deployment[]>([]);
  const [editing, setEditing] = useState<Deployment | null>(null);
  const [form, setForm] = useState(emptyForm);
  const [originDraft, setOriginDraft] = useState("");
  const [previewOpen, setPreviewOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [loading, setLoading] = useState(false);
  const [modelOptions, setModelOptions] = useState<ModelOption[]>([]);
  const [defaultModel, setDefaultModel] = useState("");

  // ---- Load properties ----
  useEffect(() => {
    fetch("/admin/api/properties")
      .then((r) => r.json())
      .then((data) => {
        const props: Property[] = data?.properties || [];
        setProperties(props);
        if (props.length && !selectedHotel) setSelectedHotel(props[0].code);
      })
      .catch(() => toast({ title: "Failed to load properties", variant: "destructive" }));
  }, []);

  // ---- Load the model list ----
  // Same source of truth as the /chat harness dropdown (model_override_service.
  // AVAILABLE_MODELS), so adding a model stays a one-line change there.
  useEffect(() => {
    fetch("/api/chat/model-override")
      .then((r) => r.json())
      .then((data) => {
        setModelOptions(Array.isArray(data?.available) ? data.available : []);
        setDefaultModel(String(data?.default_model || ""));
      })
      .catch(() => {
        /* non-critical: the picker just falls back to "follow default" only */
      });
  }, []);

  // ---- Load deployments for selected hotel ----
  useEffect(() => {
    if (!selectedHotel) return;
    setLoading(true);
    fetch(`/admin/api/widget/deployments?hotel_code=${encodeURIComponent(selectedHotel)}`)
      .then((r) => r.json())
      .then((data) => setDeployments(data?.deployments || []))
      .catch(() => toast({ title: "Failed to load deployments", variant: "destructive" }))
      .finally(() => setLoading(false));
  }, [selectedHotel]);

  // ---- Form sync when selecting a deployment ----
  useEffect(() => {
    if (!editing) {
      setForm({ ...emptyForm, hotel_code: selectedHotel });
      return;
    }
    setForm({
      hotel_code: editing.hotel_code,
      name: editing.name,
      status: editing.status,
      allowed_origins: editing.allowed_origins,
      theme: editing.theme,
      size: editing.size,
      position: editing.position,
      bot_name: editing.bot_name,
      phase: editing.phase,
      auto_open: editing.auto_open,
      logo_url: editing.logo_url,
      session_ttl_hours: editing.session_ttl_hours ?? 24,
      idle_timeout_minutes: editing.idle_timeout_minutes ?? 30,
      model: editing.model ?? "",
    });
  }, [editing, selectedHotel]);

  // ---- CRUD ----
  async function reload() {
    if (!selectedHotel) return;
    const r = await fetch(`/admin/api/widget/deployments?hotel_code=${encodeURIComponent(selectedHotel)}`);
    const data = await r.json();
    setDeployments(data?.deployments || []);
  }

  async function saveDeployment() {
    if (!form.hotel_code) {
      toast({ title: "Pick a hotel first", variant: "destructive" });
      return;
    }
    const url = editing
      ? `/admin/api/widget/deployments/${editing.widget_key}`
      : `/admin/api/widget/deployments`;
    const method = editing ? "PUT" : "POST";
    const r = await fetch(url, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(form),
    });
    if (!r.ok) {
      toast({ title: "Save failed", description: await r.text(), variant: "destructive" });
      return;
    }
    const saved: Deployment = await r.json();
    toast({ title: editing ? "Deployment updated" : "Deployment created" });
    setEditing(saved);
    setCreating(false);
    await reload();
  }

  async function deleteDeployment(key: string) {
    if (!confirm("Delete this widget deployment? Embedded sites will stop loading.")) return;
    const r = await fetch(`/admin/api/widget/deployments/${key}`, { method: "DELETE" });
    if (!r.ok) {
      toast({ title: "Delete failed", variant: "destructive" });
      return;
    }
    toast({ title: "Deleted" });
    if (editing?.widget_key === key) setEditing(null);
    await reload();
  }

  async function rotateKey(key: string) {
    if (!confirm("Rotate the widget key? The old key stops working immediately and embedded sites must update their snippet.")) return;
    const r = await fetch(`/admin/api/widget/deployments/${key}/rotate-key`, { method: "POST" });
    if (!r.ok) {
      toast({ title: "Rotation failed", variant: "destructive" });
      return;
    }
    const rotated: Deployment = await r.json();
    toast({ title: "Key rotated", description: rotated.widget_key });
    setEditing(rotated);
    await reload();
  }

  function addOrigin() {
    const v = originDraft.trim();
    if (!v) return;
    if (form.allowed_origins.includes(v)) {
      setOriginDraft("");
      return;
    }
    setForm({ ...form, allowed_origins: [...form.allowed_origins, v] });
    setOriginDraft("");
  }

  function removeOrigin(value: string) {
    setForm({ ...form, allowed_origins: form.allowed_origins.filter((o) => o !== value) });
  }

  function copy(text: string, label: string) {
    navigator.clipboard.writeText(text);
    toast({ title: `${label} copied` });
  }

  // ---- Live preview snippet ----
  const previewHtml = useMemo(() => {
    if (!editing) return "";
    return `<!doctype html>
<html><head><meta charset="utf-8"><title>Preview</title>
<style>body{font-family:system-ui;margin:0;padding:24px;background:#f8fafc;color:#0f172a}</style>
</head><body>
<h2>Mock host site</h2>
<p>Click the launcher in the corner to test the widget.</p>
<script src="${loaderSrc()}" data-widget-key="${editing.widget_key}" async></script>
</body></html>`;
  }, [editing]);

  return (
    <div className="flex-1 overflow-auto min-h-0">
    <div className="space-y-6 p-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Widget Deployments</h1>
          <p className="text-sm text-muted-foreground">
            Generate embed snippets, customize branding, and control which websites can load the chat widget.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <div className="w-56">
            <Select value={selectedHotel} onValueChange={(v) => { setSelectedHotel(v); setEditing(null); setCreating(false); }}>
              <SelectTrigger><SelectValue placeholder="Select hotel" /></SelectTrigger>
              <SelectContent>
                {properties.map((p) => (
                  <SelectItem key={p.code} value={p.code}>{p.name} ({p.code})</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <Button
            onClick={() => { setEditing(null); setCreating(true); setForm({ ...emptyForm, hotel_code: selectedHotel }); }}
            disabled={!selectedHotel}
          >
            <Plus className="mr-2 h-4 w-4" /> New Deployment
          </Button>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-[360px_1fr]">
        {/* ---- Deployment list ---- */}
        <Card>
          <CardHeader><CardTitle>Deployments</CardTitle></CardHeader>
          <CardContent className="space-y-2">
            {loading && <div className="text-sm text-muted-foreground">Loading…</div>}
            {!loading && deployments.length === 0 && (
              <div className="text-sm text-muted-foreground">No deployments yet. Create one to get an embed snippet.</div>
            )}
            {deployments.map((d) => (
              <button
                key={d.widget_key}
                onClick={() => { setEditing(d); setCreating(false); }}
                className={`w-full text-left rounded-md border p-3 transition hover:bg-muted/50 ${editing?.widget_key === d.widget_key ? "border-primary bg-muted/50" : ""}`}
              >
                <div className="flex items-center justify-between">
                  <span className="font-medium">{d.name || d.widget_key}</span>
                  <Badge variant={d.status === "active" ? "default" : "secondary"}>{d.status}</Badge>
                </div>
                <div className="mt-1 truncate font-mono text-xs text-muted-foreground">{d.widget_key}</div>
                <div className="mt-1 text-xs text-muted-foreground">{d.allowed_origins.length} allowed origin(s)</div>
                <div className="mt-1 text-xs">
                  {d.model ? (
                    <span className="text-muted-foreground">
                      Model: <span className="font-mono">{d.model}</span> (pinned)
                    </span>
                  ) : (
                    <span className="text-amber-600 dark:text-amber-500">
                      Model: not pinned — follows the test harness
                    </span>
                  )}
                </div>
              </button>
            ))}
          </CardContent>
        </Card>

        {/* ---- Editor ---- */}
        {(editing || creating) ? (
          <div className="space-y-6">
            <Card>
              <CardHeader>
                <CardTitle>{editing ? "Edit deployment" : "New deployment"}</CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="grid gap-4 sm:grid-cols-2">
                  <div>
                    <Label>Name</Label>
                    <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="e.g. Main website" />
                  </div>
                  <div>
                    <Label>Bot name</Label>
                    <Input value={form.bot_name} onChange={(e) => setForm({ ...form, bot_name: e.target.value })} />
                  </div>
                  <div>
                    <Label>Status</Label>
                    <Select value={form.status} onValueChange={(v) => setForm({ ...form, status: v as "active" | "inactive" })}>
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="active">Active</SelectItem>
                        <SelectItem value="inactive">Inactive</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div>
                    <Label>Position</Label>
                    <Select value={form.position} onValueChange={(v) => setForm({ ...form, position: v as "left" | "right" })}>
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="right">Bottom right</SelectItem>
                        <SelectItem value="left">Bottom left</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div>
                    <Label>Phase</Label>
                    <Input value={form.phase} onChange={(e) => setForm({ ...form, phase: e.target.value })} />
                  </div>
                  <div className="flex items-center justify-between">
                    <Label>Auto-open on load</Label>
                    <Switch checked={form.auto_open} onCheckedChange={(v) => setForm({ ...form, auto_open: v })} />
                  </div>
                  <div>
                    <Label>Session expiry</Label>
                    <Select
                      value={String(form.session_ttl_hours)}
                      onValueChange={(v) => setForm({ ...form, session_ttl_hours: Number(v) })}
                    >
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="1">1 hour</SelectItem>
                        <SelectItem value="6">6 hours</SelectItem>
                        <SelectItem value="12">12 hours</SelectItem>
                        <SelectItem value="24">24 hours (default)</SelectItem>
                        <SelectItem value="48">48 hours</SelectItem>
                        <SelectItem value="168">1 week</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div>
                    <Label>Idle timeout</Label>
                    <Select
                      value={String(form.idle_timeout_minutes)}
                      onValueChange={(v) => setForm({ ...form, idle_timeout_minutes: Number(v) })}
                    >
                      <SelectTrigger><SelectValue /></SelectTrigger>
                      <SelectContent>
                        <SelectItem value="0">Disabled</SelectItem>
                        <SelectItem value="5">5 minutes</SelectItem>
                        <SelectItem value="15">15 minutes</SelectItem>
                        <SelectItem value="30">30 minutes (default)</SelectItem>
                        <SelectItem value="60">1 hour</SelectItem>
                        <SelectItem value="120">2 hours</SelectItem>
                        <SelectItem value="240">4 hours</SelectItem>
                    </SelectContent>
                    </Select>
                    <p className="mt-1 text-xs text-muted-foreground">Resets the chat after this much user inactivity.</p>
                  </div>
                </div>

                {/* AI model pin */}
                <div>
                  <Label>AI model</Label>
                  <p className="mb-1.5 text-xs text-muted-foreground">
                    Locks this embed to one model. Pinned embeds also use that model's
                    prompts, and are unaffected by the model selector in the chat test
                    harness — so testing never changes what live guests get.
                  </p>
                  <Select
                    value={form.model || UNPINNED_MODEL}
                    onValueChange={(v) =>
                      setForm({ ...form, model: v === UNPINNED_MODEL ? "" : v })
                    }
                  >
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value={UNPINNED_MODEL}>
                        Not pinned — follows the test harness{defaultModel ? ` (now: ${defaultModel})` : ""}
                      </SelectItem>
                      {modelOptions.map((m) => (
                        <SelectItem key={m.id} value={m.id}>{m.label}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  {!form.model && (
                    <p className="mt-1 text-xs text-amber-600 dark:text-amber-500">
                      Unpinned: switching the model in the chat harness will change what
                      guests on this embed get.
                    </p>
                  )}
                </div>

                {/* Logo */}
                <div>
                  <Label>Logo URL</Label>
                  <p className="mb-1.5 text-xs text-muted-foreground">Paste a URL or upload an image file.</p>
                  <div className="flex gap-2 items-center">
                    <Input
                      value={form.logo_url}
                      onChange={(e) => setForm({ ...form, logo_url: e.target.value })}
                      placeholder="https://example.com/logo.png"
                      className="flex-1 text-xs"
                    />
                    <label className="cursor-pointer">
                      <span className="inline-flex items-center gap-1.5 h-9 px-3 text-xs border rounded-md bg-background hover:bg-muted/50 transition-colors font-medium whitespace-nowrap">
                        <Upload className="h-3.5 w-3.5" /> Upload
                      </span>
                      <input
                        type="file"
                        accept=".png,.jpg,.jpeg,.svg,.webp,.gif"
                        className="hidden"
                        onChange={async (e) => {
                          const file = e.target.files?.[0];
                          if (!file) return;
                          const fd = new FormData();
                          fd.append("file", file);
                          try {
                            const r = await fetch("/admin/api/widget/logo-upload", { method: "POST", body: fd });
                            const data = await r.json();
                            if (data.url) setForm({ ...form, logo_url: data.url });
                          } catch {
                            toast({ title: "Upload failed", variant: "destructive" });
                          }
                        }}
                      />
                    </label>
                    {form.logo_url && (
                      <img src={form.logo_url} alt="Logo preview" className="h-9 w-auto max-w-[80px] rounded border object-contain" />
                    )}
                  </div>
                </div>

                <div className="grid gap-4 sm:grid-cols-4">
                  {(["brand_color", "accent_color", "bg_color", "text_color"] as const).map((k) => (
                    <div key={k}>
                      <Label className="capitalize">{k.replace("_", " ")}</Label>
                      <div className="mt-1 flex gap-2">
                        <input
                          type="color"
                          value={form.theme[k]}
                          onChange={(e) => setForm({ ...form, theme: { ...form.theme, [k]: e.target.value } })}
                          className="h-9 w-12 cursor-pointer rounded border"
                        />
                        <Input
                          value={form.theme[k]}
                          onChange={(e) => setForm({ ...form, theme: { ...form.theme, [k]: e.target.value } })}
                          className="font-mono text-xs"
                        />
                      </div>
                    </div>
                  ))}
                </div>

                <div className="grid gap-4 sm:grid-cols-2">
                  <div>
                    <Label>Width (280–600)</Label>
                    <Input type="number" min={280} max={600} value={form.size.width}
                      onChange={(e) => setForm({ ...form, size: { ...form.size, width: Number(e.target.value) } })} />
                  </div>
                  <div>
                    <Label>Height (360–900)</Label>
                    <Input type="number" min={360} max={900} value={form.size.height}
                      onChange={(e) => setForm({ ...form, size: { ...form.size, height: Number(e.target.value) } })} />
                  </div>
                </div>

                <div>
                  <Label>Allowed origins</Label>
                  <p className="mb-2 text-xs text-muted-foreground">
                    Domains permitted to embed this widget. Use full origins like <code>https://hotelx.com</code>. Leave empty for any (dev only).
                  </p>
                  <div className="mb-2 flex gap-2">
                    <Input
                      value={originDraft}
                      onChange={(e) => setOriginDraft(e.target.value)}
                      placeholder="https://hotelx.com"
                      onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addOrigin(); } }}
                    />
                    <Button type="button" variant="secondary" onClick={addOrigin}>Add</Button>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {form.allowed_origins.map((o) => (
                      <Badge key={o} variant="outline" className="cursor-pointer" onClick={() => removeOrigin(o)}>
                        {o} ✕
                      </Badge>
                    ))}
                  </div>
                </div>

                <div className="flex flex-wrap gap-2 pt-2">
                  <Button onClick={saveDeployment}>{editing ? "Save changes" : "Create deployment"}</Button>
                  {editing && (
                    <>
                      <Button variant="secondary" onClick={() => setPreviewOpen(true)}>
                        <Eye className="mr-2 h-4 w-4" /> Preview
                      </Button>
                      <Button variant="secondary" onClick={() => rotateKey(editing.widget_key)}>
                        <RefreshCw className="mr-2 h-4 w-4" /> Rotate Key
                      </Button>
                      <Button variant="destructive" onClick={() => deleteDeployment(editing.widget_key)}>
                        <Trash2 className="mr-2 h-4 w-4" /> Delete
                      </Button>
                    </>
                  )}
                  <Button variant="ghost" onClick={() => { setEditing(null); setCreating(false); }}>Cancel</Button>
                </div>
              </CardContent>
            </Card>

            {/* ---- Snippets ---- */}
            {editing && (
              <Card>
                <CardHeader>
                  <CardTitle className="flex items-center justify-between">
                    Install snippet
                    <code className="rounded bg-muted px-2 py-1 text-xs font-mono">{editing.widget_key}</code>
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  <Tabs defaultValue="html">
                    <TabsList>
                      <TabsTrigger value="html">HTML</TabsTrigger>
                      <TabsTrigger value="react">React</TabsTrigger>
                      <TabsTrigger value="next">Next.js</TabsTrigger>
                      <TabsTrigger value="vue">Vue</TabsTrigger>
                      <TabsTrigger value="angular">Angular</TabsTrigger>
                      <TabsTrigger value="wordpress">WordPress / Shopify</TabsTrigger>
                    </TabsList>
                    {[
                      { key: "html", label: "HTML", code: htmlSnippet(editing) },
                      { key: "react", label: "React", code: reactSnippet(editing) },
                      { key: "next", label: "Next.js", code: nextSnippet(editing) },
                      { key: "vue", label: "Vue", code: vueSnippet(editing) },
                      { key: "angular", label: "Angular", code: angularSnippet(editing) },
                      { key: "wordpress", label: "WordPress / Shopify", code: wordpressSnippet(editing) },
                    ].map((t) => (
                      <TabsContent key={t.key} value={t.key}>
                        <div className="relative">
                          <pre className="max-h-96 overflow-auto rounded-md bg-muted/50 p-4 text-xs">
                            <code>{t.code}</code>
                          </pre>
                          <Button
                            size="sm"
                            variant="secondary"
                            className="absolute right-2 top-2"
                            onClick={() => copy(t.code, t.label)}
                          >
                            <Copy className="mr-2 h-3 w-3" /> Copy
                          </Button>
                        </div>
                      </TabsContent>
                    ))}
                  </Tabs>
                </CardContent>
              </Card>
            )}
          </div>
        ) : (
          <Card>
            <CardContent className="flex h-64 items-center justify-center text-sm text-muted-foreground">
              Select a deployment to edit, or create a new one.
            </CardContent>
          </Card>
        )}
      </div>

      {/* ---- Widget Component Registry ---- */}
      <Card>
        <CardHeader>
          <CardTitle>Widget Component Registry</CardTitle>
          <p className="text-sm text-muted-foreground">
            All registered widget components. Configurable components can be swapped per deployment in future releases.
          </p>
        </CardHeader>
        <CardContent>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {componentRegistry.list().map((c) => (
              <div key={c.key} className="rounded-md border p-3 text-sm">
                <div className="flex items-center gap-2">
                  <span className="font-medium">{c.name}</span>
                  {c.configurable && (
                    <Badge variant="outline" className="text-[10px]">configurable</Badge>
                  )}
                  {c.mobileAdaptive && (
                    <Badge variant="secondary" className="text-[10px]">mobile-adaptive</Badge>
                  )}
                </div>
                <p className="mt-1 text-xs text-muted-foreground">{c.description}</p>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* ---- Live Preview Dialog ---- */}
      <Dialog open={previewOpen} onOpenChange={setPreviewOpen}>
        <DialogContent className="max-w-5xl">
          <DialogHeader>
            <DialogTitle>Live preview</DialogTitle>
            <DialogDescription>
              The widget loads inside a sandboxed iframe using your saved configuration. To test on a real site, paste the snippet into the host page.
            </DialogDescription>
          </DialogHeader>
          <div className="h-[600px] w-full overflow-hidden rounded-md border bg-white">
            <iframe
              title="Widget preview"
              srcDoc={previewHtml}
              className="h-full w-full"
              sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
            />
          </div>
          <DialogFooter>
            <Button variant="secondary" onClick={() => window.open(`/static/embed/preview.html?widget_key=${editing?.widget_key || ""}`, "_blank")}>
              <ExternalLink className="mr-2 h-4 w-4" /> Open in new tab
            </Button>
            <Button onClick={() => setPreviewOpen(false)}>Close</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
    </div>
  );
}
