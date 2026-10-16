import { useEffect, useRef, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Checkbox } from "@/components/ui/checkbox";
import { Copy, Check, Eye, Upload } from "lucide-react";
import { toast } from "sonner";
import { UNPINNED_MODEL, type ModelOption } from "@/lib/widgetModel";

interface ChannelsData {
  primaryColor: string;
  accentColor: string;
  bgColor: string;
  textColor: string;
  logoUrl: string;
  widgetPosition: string;
  widgetWidth: number;
  widgetHeight: number;
  industryFeatures: string;
  webEnabled: boolean;
  whatsappEnabled: boolean;
}

interface ChannelsStepProps {
  data: ChannelsData;
  propertyCode: string;
  botName: string;
  onChange: <K extends keyof ChannelsData>(field: K, value: ChannelsData[K]) => void;
  onSave: () => void;
}

const ChannelsStep = ({ data, propertyCode, botName, onChange, onSave }: ChannelsStepProps) => {
  const [copied, setCopied] = useState(false);
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const widgetHost = typeof window === "undefined" ? "https://your-widget-host.com" : window.location.origin;
  const normalizedPropertyCode = String(propertyCode || "default").trim().toLowerCase() || "default";
  const resolvedBotName = String(botName || "Assistant").trim() || "Assistant";
  const previewWidth = 380;
  const previewHeight = 560;

  // This hotel's active deployment key. A snippet carrying data-widget-key is
  // bound to a deployment row, which is what lets Admin > Widget pin its AI
  // model (and shield it from the chat harness dropdown). A hotel-code-only
  // snippet has no deployment identity and therefore can NOT be pinned, so we
  // emit the key whenever one exists and warn when it doesn't.
  const [widgetKey, setWidgetKey] = useState("");
  const [pinnedModel, setPinnedModel] = useState("");
  const [modelOptions, setModelOptions] = useState<ModelOption[]>([]);
  const [savingModel, setSavingModel] = useState(false);

  useEffect(() => {
    if (!normalizedPropertyCode) return;
    let cancelled = false;
    fetch(`/admin/api/widget/deployments?hotel_code=${encodeURIComponent(normalizedPropertyCode)}`)
      .then((r) => r.json())
      .then((d) => {
        if (cancelled) return;
        const active = (d?.deployments || []).filter(
          (x: { status?: string }) => x?.status === "active",
        );
        // list() orders created_at asc -> newest last, matching the server's
        // own "which deployment represents this hotel" choice.
        const dep = active.length ? active[active.length - 1] : null;
        setWidgetKey(dep ? String(dep.widget_key || "") : "");
        setPinnedModel(dep ? String(dep.model || "") : "");
      })
      .catch(() => {
        if (!cancelled) {
          setWidgetKey("");
          setPinnedModel("");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [normalizedPropertyCode]);

  // Same list the chat harness dropdown uses (model_override_service.AVAILABLE_MODELS).
  useEffect(() => {
    fetch("/api/chat/model-override")
      .then((r) => r.json())
      .then((d) => setModelOptions(Array.isArray(d?.available) ? d.available : []))
      .catch(() => {
        /* non-critical */
      });
  }, []);

  // The model is saved to the DEPLOYMENT ROW, never written into the snippet:
  // the snippet lives on the customer's site where anyone could edit it, and the
  // model decides what gets billed. Storing it server-side also means changing
  // the model later is one click here — no customer has to re-paste anything.
  const saveModel = async (next: string) => {
    if (!widgetKey) return;
    const previous = pinnedModel;
    setPinnedModel(next);
    setSavingModel(true);
    try {
      const r = await fetch(`/admin/api/widget/deployments/${widgetKey}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: next }),
      });
      if (!r.ok) throw new Error(await r.text());
      toast.success(next ? `Embed pinned to ${next}` : "Embed unpinned");
    } catch (err) {
      setPinnedModel(previous);
      toast.error("Could not save the model for this embed");
    } finally {
      setSavingModel(false);
    }
  };

  const embedCode = `<script src="${widgetHost}/static/embed/kebo-widget-loader.js"
  data-widget-id="${normalizedPropertyCode}"
${widgetKey ? `  data-widget-key="${widgetKey}"\n` : ""}  data-hotel-code="${normalizedPropertyCode}"
  data-phase="pre_booking"
  data-brand-color="${data.primaryColor}"
  data-accent-color="${data.accentColor}"
  data-bg-color="${data.bgColor}"
  data-text-color="${data.textColor}"
  data-bot-name="${resolvedBotName}"
  data-position="${data.widgetPosition}">
</script>`;

  const handleCopy = () => {
    navigator.clipboard.writeText(embedCode);
    setCopied(true);
    toast.success("Embed code copied to clipboard!");
    setTimeout(() => setCopied(false), 2000);
  };

  const handleLogoUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    e.target.value = "";
    setUploading(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const r = await fetch("/admin/api/widget/logo-upload", { method: "POST", body: fd });
      const result = await r.json();
      if (result.url) {
        onChange("logoUrl", result.url);
        toast.success("Logo uploaded");
      } else {
        toast.error("Upload failed");
      }
    } catch {
      toast.error("Upload failed");
    } finally {
      setUploading(false);
    }
  };

  return (
  <Card>
    <CardHeader className="rounded-t-lg bg-primary px-6 py-4">
      <div className="flex items-center justify-between">
        <CardTitle className="text-lg text-primary-foreground">Step 3: Channels, Colors & Features</CardTitle>
        <Button size="sm" variant="secondary" onClick={onSave}>Save</Button>
      </div>
    </CardHeader>
    <CardContent className="space-y-6 p-6">
      <div className="grid gap-4 lg:grid-cols-[1fr_auto] lg:items-start">
        <div>
          <Label className="mb-3 block">Brand Colors</Label>
          <div className="grid grid-cols-2 gap-4">
            {([
              { key: "primaryColor" as const, label: "Primary" },
              { key: "accentColor" as const, label: "Accent" },
              { key: "bgColor" as const, label: "Background" },
              { key: "textColor" as const, label: "Text" },
            ]).map(({ key, label }) => (
              <div key={key} className="space-y-1.5 rounded-md border bg-white p-2">
                <Label className="text-xs">{label}</Label>
                <div className="flex items-center gap-2">
                  <input
                    type="color"
                    value={data[key]}
                    onChange={(e) => onChange(key, e.target.value)}
                    className="h-9 w-12 cursor-pointer rounded border border-input bg-transparent"
                  />
                  <span className="text-xs text-muted-foreground">{data[key]}</span>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="space-y-2 lg:w-[420px]">
          <Label className="mb-2 flex items-center gap-2">
            <Eye className="h-4 w-4" />
            Widget Preview
          </Label>
          <p className="text-xs text-muted-foreground">
            Live preview of current color configuration.
          </p>
          <div className="rounded-xl border bg-slate-100 p-3">
            <div
              className="overflow-hidden rounded-2xl border border-slate-300 shadow-xl"
              style={{ width: previewWidth, height: previewHeight }}
            >
              <div
                className="flex items-center justify-between px-4 py-3"
                style={{ backgroundColor: data.primaryColor }}
              >
                <div className="flex items-center gap-2">
                  {data.logoUrl ? (
                    <img
                      src={data.logoUrl}
                      alt={resolvedBotName}
                      className="h-7 w-7 rounded-full object-cover border border-white/30 flex-shrink-0"
                      onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }}
                    />
                  ) : (
                    <div
                      className="h-7 w-7 rounded-full flex items-center justify-center text-xs font-bold flex-shrink-0"
                      style={{ backgroundColor: `${data.primaryColor}66`, color: "#fff" }}
                    >
                      {resolvedBotName[0] || "A"}
                    </div>
                  )}
                  <div>
                    <p className="text-sm font-semibold text-white">{resolvedBotName}</p>
                    <p className="text-[11px] text-white/80">{normalizedPropertyCode} • pre_booking</p>
                  </div>
                </div>
                <button
                  type="button"
                  className="rounded-md px-2 py-1 text-xs text-white/90"
                  style={{ backgroundColor: `${data.primaryColor}66` }}
                >
                  Close
                </button>
              </div>

              <div
                className="flex h-[calc(100%-56px)] flex-col"
                style={{ backgroundColor: data.bgColor, color: data.textColor }}
              >
                <div className="flex-1 space-y-2 overflow-auto p-3 text-sm">
                  <div className="max-w-[82%] rounded-2xl rounded-bl-sm bg-white px-3 py-2 shadow-sm">
                    Hi! How can I help you today?
                  </div>
                  <div className="flex justify-end">
                    <div
                      className="max-w-[72%] rounded-2xl rounded-br-sm px-3 py-2 text-white shadow-sm"
                      style={{ backgroundColor: data.primaryColor }}
                    >
                      Hi
                    </div>
                  </div>
                  <div className="max-w-[82%] rounded-2xl rounded-bl-sm bg-white px-3 py-2 shadow-sm text-slate-500">
                    ...
                  </div>
                </div>
                <div className="border-t border-slate-200 bg-white p-3">
                  <div className="flex items-center gap-2">
                    <input
                      readOnly
                      value="Type a message..."
                      className="h-10 flex-1 rounded-lg border px-3 text-sm"
                      style={{ borderColor: `${data.primaryColor}66`, color: data.textColor }}
                    />
                    <button
                      type="button"
                      className="flex h-10 w-10 items-center justify-center rounded-full text-white"
                      style={{ backgroundColor: data.primaryColor }}
                    >
                      →
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Logo upload */}
      <div className="space-y-2">
        <Label>Bot Logo</Label>
        <p className="text-xs text-muted-foreground">Shown in the widget header. Paste a URL or upload an image.</p>
        <div className="flex items-center gap-2">
          <Input
            value={data.logoUrl}
            onChange={(e) => onChange("logoUrl", e.target.value)}
            placeholder="https://example.com/logo.png"
            className="flex-1 text-xs"
          />
          <input
            type="file"
            ref={fileInputRef}
            accept=".png,.jpg,.jpeg,.svg,.webp,.gif"
            className="hidden"
            onChange={handleLogoUpload}
          />
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={uploading}
            onClick={() => fileInputRef.current?.click()}
          >
            <Upload className="mr-1.5 h-3.5 w-3.5" />
            {uploading ? "Uploading…" : "Upload"}
          </Button>
          {data.logoUrl && (
            <img
              src={data.logoUrl}
              alt="Logo"
              className="h-9 w-auto max-w-[72px] rounded border object-contain"
              onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = "none"; }}
            />
          )}
        </div>
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-1">
        <div className="space-y-1.5">
          <Label>Widget Position</Label>
          <Select value={data.widgetPosition} onValueChange={(v) => onChange("widgetPosition", v)}>
            <SelectTrigger><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="right">Bottom Right</SelectItem>
              <SelectItem value="left">Bottom Left</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>

      <div className="space-y-1.5">
        <Label>Industry Features</Label>
        <Textarea
          value={data.industryFeatures}
          readOnly
          rows={4}
          className="bg-muted/50 cursor-default focus-visible:ring-0"
          placeholder="Select an industry in Step 1 to auto-populate features"
        />
        <p className="text-xs text-muted-foreground">Auto-populated based on industry selection</p>
      </div>

      <div>
        <Label className="mb-3 block">Channel Enablement</Label>
        <div className="flex flex-wrap gap-6">
          <label className="flex items-center gap-2">
            <Checkbox checked={data.webEnabled} onCheckedChange={(v) => onChange("webEnabled", !!v)} />
            <span className="text-sm">Web Widget Enabled</span>
          </label>
        </div>
      </div>

      <div className="space-y-2">
        {/* AI model for this embed */}
        <div className="mb-4">
          <Label className="mb-2 block">AI model for this embed</Label>
          <p className="text-xs text-muted-foreground mb-2">
            The embed code below will always use this model — and this model's prompts —
            for real guests. Changing the model in the chat test harness will not affect
            it. Takes effect immediately; nobody has to re-paste the code.
          </p>
          <Select
            value={pinnedModel || UNPINNED_MODEL}
            onValueChange={(v) => saveModel(v === UNPINNED_MODEL ? "" : v)}
            disabled={!widgetKey || savingModel}
          >
            <SelectTrigger className="max-w-md"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value={UNPINNED_MODEL}>Not pinned — follows the test harness</SelectItem>
              {modelOptions.map((m) => (
                <SelectItem key={m.id} value={m.id}>{m.label}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          {widgetKey && !pinnedModel && (
            <p className="mt-1 text-xs text-amber-600 dark:text-amber-500">
              Not pinned: switching the model in the chat harness will change what real
              guests on this embed get.
            </p>
          )}
        </div>

        <Label className="mb-2 block">Embed Code</Label>
        <p className="text-xs text-muted-foreground mb-2">
          Copy and paste this code into your website's HTML to add the Kebo Bot widget.
        </p>
        {!widgetKey && (
          <p className="text-xs text-amber-600 dark:text-amber-500 mb-2">
            No active widget deployment for this property, so this snippet can't be
            pinned to an AI model — it will follow whatever the chat test harness is
            set to. Create a deployment in Admin &gt; Widget to pin it.
          </p>
        )}
        <div className="relative">
          <pre className="rounded-lg border bg-muted/50 p-3 text-xs font-mono overflow-x-auto whitespace-pre-wrap">
            {embedCode}
          </pre>
          <Button
            size="sm"
            variant="outline"
            className="absolute right-2 top-2 h-7 gap-1"
            onClick={handleCopy}
          >
            {copied ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
            {copied ? "Copied" : "Copy"}
          </Button>
        </div>
      </div>
    </CardContent>
  </Card>
  );
};

export default ChannelsStep;
export type { ChannelsData };

