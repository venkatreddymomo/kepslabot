import { useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Card, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Plus, Trash2, Save, MessageSquare, Loader2, Upload, Download, CheckCircle2, XCircle } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { adminFetch, adminGet, adminSend } from "@/lib/adminApi";

interface FaqEntry {
  id: string;
  question: string;
  answer: string;
  // Property scope. Empty propertySlug = COMMON to all properties under the
  // hotel; a slug scopes the FAQ to that one property. propertyId is carried for
  // round-trip; the runtime keys off propertySlug.
  propertySlug: string;
  propertyId: number | null;
}

interface SubProperty {
  id: number;
  slug: string;
  name: string;
  is_default: boolean;
}

interface BulkUploadResult {
  added: number;
  skipped: number;
  failed: number;
  errors: string[];
}

interface FaqToolsTabProps {
  propertyCode: string;
}

const CSV_TEMPLATE = "question,answer,description\nWhat time is check-in?,Check-in is from 2 PM onwards.,Standard check-in policy\nWhat is the WiFi password?,The WiFi password is Welcome2025.,Updated monthly\n";

function downloadTemplate() {
  const blob = new Blob([CSV_TEMPLATE], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "faq_template.csv";
  a.click();
  URL.revokeObjectURL(url);
}

const FaqToolsTab = ({ propertyCode }: FaqToolsTabProps) => {
  const { toast } = useToast();
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadResult, setUploadResult] = useState<BulkUploadResult | null>(null);
  // Full list (every scope). The view below filters this; keeping the full list
  // means the save/delete reconciliation never deletes another property's FAQs.
  const [faqs, setFaqs] = useState<FaqEntry[]>([]);

  // Sub-properties under the active hotel. When the hotel has more than zero, we
  // show a "Select Property" picker (matches the Content Manager pattern).
  const [subProperties, setSubProperties] = useState<SubProperty[]>([]);
  // Union value: "" = All, "shared" = Common (no property), "<slug>" = specific.
  const [scopeValue, setScopeValue] = useState<string>("");

  const faqIds = useMemo(
    () => new Set(faqs.map((f) => f.id).filter((id) => !id.startsWith("tmp-"))),
    [faqs],
  );

  // Resolve the property_id that goes with a slug (for round-tripping to the API).
  const slugToId = useMemo(() => {
    const m = new Map<string, number>();
    for (const p of subProperties) if (p.slug) m.set(p.slug, p.id);
    return m;
  }, [subProperties]);

  // The scope new/bulk entries inherit + the filter applied to the list.
  //   "" / "shared"  -> common (empty slug)
  //   "<slug>"       -> that property
  const writeScope = useMemo(() => {
    if (!scopeValue || scopeValue === "shared") return { slug: "", id: null as number | null };
    return { slug: scopeValue, id: slugToId.get(scopeValue) ?? null };
  }, [scopeValue, slugToId]);

  const visibleFaqs = useMemo(() => {
    if (!scopeValue) return faqs; // All
    if (scopeValue === "shared") return faqs.filter((f) => !f.propertySlug);
    return faqs.filter((f) => f.propertySlug === scopeValue);
  }, [faqs, scopeValue]);

  const scopeLabel = (f: FaqEntry) => {
    if (!f.propertySlug) return "Common";
    const match = subProperties.find((p) => p.slug === f.propertySlug);
    return match ? match.name : f.propertySlug;
  };

  const loadSubProperties = async () => {
    try {
      const payload = await adminGet<{ properties?: SubProperty[] }>("/hotel-properties", propertyCode);
      setSubProperties(Array.isArray(payload?.properties) ? payload.properties : []);
    } catch {
      setSubProperties([]);
    }
  };

  const loadData = async () => {
    setLoading(true);
    try {
      const faqRows = await adminGet<Array<Record<string, unknown>>>("/config/faq-bank", propertyCode);
      setFaqs(
        (Array.isArray(faqRows) ? faqRows : []).map((row) => ({
          id: String(row.id || `tmp-${crypto.randomUUID()}`),
          question: String(row.question || ""),
          answer: String(row.answer || ""),
          propertySlug: String(row.property_slug || ""),
          propertyId:
            row.property_id === null || row.property_id === undefined || row.property_id === ""
              ? null
              : Number(row.property_id),
        })),
      );
    } catch (error) {
      toast({
        title: "Failed to load FAQ",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    // Reset the scope filter whenever the hotel changes — a slug from a previous
    // hotel is meaningless under a different one.
    setScopeValue("");
    void loadSubProperties();
    void loadData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [propertyCode]);

  const addFaq = () =>
    setFaqs((prev) => [
      ...prev,
      {
        id: `tmp-${crypto.randomUUID()}`,
        question: "",
        answer: "",
        propertySlug: writeScope.slug,
        propertyId: writeScope.id,
      },
    ]);

  const removeFaq = (id: string) => setFaqs((prev) => prev.filter((entry) => entry.id !== id));

  const updateFaq = (id: string, field: "question" | "answer", value: string) =>
    setFaqs((prev) => prev.map((entry) => (entry.id === id ? { ...entry, [field]: value } : entry)));

  const handleSave = async () => {
    setSaving(true);
    try {
      const serverFaqs = await adminGet<Array<Record<string, unknown>>>("/config/faq-bank", propertyCode);
      const serverFaqIds = new Set(
        (Array.isArray(serverFaqs) ? serverFaqs : []).map((row) => String(row.id || "")),
      );

      // Operate on the FULL list so cross-property entries are preserved.
      for (const faq of faqs) {
        if (!faq.question.trim() || !faq.answer.trim()) continue;
        const payload = {
          question: faq.question.trim(),
          answer: faq.answer.trim(),
          // Always send the entry's own scope so an edit keeps it where it is.
          property_slug: faq.propertySlug || "",
          property_id: faq.propertyId,
          ...(faq.id.startsWith("tmp-") ? {} : { id: faq.id }),
        };
        if (faq.id.startsWith("tmp-")) {
          await adminSend("POST", "/config/faq-bank", payload, propertyCode);
        } else {
          await adminSend("PUT", `/config/faq-bank/${encodeURIComponent(faq.id)}`, payload, propertyCode);
        }
      }

      for (const id of serverFaqIds) {
        if (!faqIds.has(id)) {
          await adminSend("DELETE", `/config/faq-bank/${encodeURIComponent(id)}`, undefined, propertyCode);
        }
      }

      await loadData();
      toast({ title: "FAQ saved" });
    } catch (error) {
      toast({
        title: "Failed to save FAQ",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setSaving(false);
    }
  };

  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!e.target.files) return;
    // Reset input so same file can be re-selected
    e.target.value = "";
    if (!file) return;

    const ext = file.name.split(".").pop()?.toLowerCase();
    if (ext !== "csv" && ext !== "json" && ext !== "xlsx" && ext !== "txt") {
      toast({ title: "Unsupported file", description: "Please upload a .csv, .json, .xlsx, or .txt file.", variant: "destructive" });
      return;
    }

    setUploading(true);
    setUploadResult(null);

    try {
      const formData = new FormData();
      formData.append("file", file);
      // Stamp the chosen scope onto every uploaded row.
      if (writeScope.slug) formData.append("property_slug", writeScope.slug);
      if (writeScope.id != null) formData.append("property_id", String(writeScope.id));

      const result = await adminFetch("/config/faq-bank/bulk-upload", {
        method: "POST",
        body: formData,
        propertyCode,
      }) as BulkUploadResult;

      setUploadResult(result);

      if (result.added > 0) {
        toast({ title: `Uploaded ${result.added} FAQ${result.added !== 1 ? "s" : ""}`, description: result.skipped > 0 ? `${result.skipped} row(s) skipped` : undefined });
        await loadData();
      } else {
        toast({ title: "No FAQs added", description: result.errors?.[0] || "Check file format.", variant: "destructive" });
      }
    } catch (error) {
      toast({
        title: "Upload failed",
        description: String(error instanceof Error ? error.message : error),
        variant: "destructive",
      });
    } finally {
      setUploading(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center py-8 text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading FAQ...
      </div>
    );
  }

  const scopeUploadingTo =
    !scopeValue || scopeValue === "shared"
      ? "Common (all properties)"
      : subProperties.find((p) => p.slug === scopeValue)?.name || scopeValue;

  return (
    <div className="space-y-6">
      {/* Property scope picker — only when the hotel actually has sub-properties. */}
      {subProperties.length > 0 && (
        <div className="space-y-1.5 rounded-md border bg-card p-4">
          <Label htmlFor="faq-property-picker">Select Property</Label>
          <select
            id="faq-property-picker"
            className="h-10 w-full rounded-md border bg-background px-3 text-sm"
            value={scopeValue}
            onChange={(event) => setScopeValue(event.target.value)}
          >
            <option value="">All FAQs (any property)</option>
            <option value="shared">Common (all properties)</option>
            {subProperties.map((p) => (
              <option key={p.id} value={p.slug}>
                {p.name}
                {p.is_default ? " (default)" : ""}
              </option>
            ))}
          </select>
          <p className="text-xs text-muted-foreground">
            Pick a property to view/edit its FAQs and scope new uploads to it. Pick
            &quot;Common&quot; to add FAQs shared by every property under this hotel.
            New &amp; uploaded entries go to:{" "}
            <span className="font-semibold">{scopeUploadingTo}</span>.
          </p>
        </div>
      )}

      {/* Header row */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="flex items-center gap-2 text-sm text-muted-foreground">
          <MessageSquare className="h-4 w-4" />
          {visibleFaqs.length > 0 ? `${visibleFaqs.length} FAQ entry${visibleFaqs.length !== 1 ? "s" : ""}` : "Add FAQ question-answer pairs."}
        </p>
        <div className="flex items-center gap-2">
          {/* Download template */}
          <Button size="sm" variant="outline" onClick={downloadTemplate} title="Download CSV template">
            <Download className="mr-2 h-4 w-4" /> Template
          </Button>
          {/* CSV/JSON upload */}
          <Button
            size="sm"
            variant="outline"
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
          >
            {uploading ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <Upload className="mr-2 h-4 w-4" />
            )}
            {uploading ? "Uploading…" : "Upload file"}
          </Button>
          <input
            ref={fileInputRef}
            type="file"
            accept=".csv,.json,.xlsx,.txt"
            className="hidden"
            onChange={handleFileChange}
          />
          {/* Add single entry */}
          <Button size="sm" onClick={addFaq}>
            <Plus className="mr-2 h-4 w-4" /> Add FAQ
          </Button>
        </div>
      </div>

      {/* Upload result banner */}
      {uploadResult && (
        <div className="rounded-md border p-3 text-sm">
          <div className="flex items-center gap-4">
            <span className="flex items-center gap-1 text-green-600">
              <CheckCircle2 className="h-4 w-4" />
              {uploadResult.added} added
            </span>
            {uploadResult.skipped > 0 && (
              <span className="text-muted-foreground">{uploadResult.skipped} skipped</span>
            )}
            {uploadResult.failed > 0 && (
              <span className="flex items-center gap-1 text-destructive">
                <XCircle className="h-4 w-4" />
                {uploadResult.failed} failed
              </span>
            )}
          </div>
          {uploadResult.errors.length > 0 && (
            <ul className="mt-2 space-y-0.5 text-xs text-muted-foreground">
              {uploadResult.errors.slice(0, 5).map((err, i) => (
                <li key={i}>• {err}</li>
              ))}
              {uploadResult.errors.length > 5 && (
                <li>…and {uploadResult.errors.length - 5} more</li>
              )}
            </ul>
          )}
        </div>
      )}

      {/* FAQ entries */}
      {visibleFaqs.map((faq) => (
        <Card key={faq.id}>
          <CardContent className="space-y-3 pt-4">
            <div className="flex items-start gap-3">
              <div className="flex-1 space-y-2">
                <Input
                  placeholder="Question"
                  value={faq.question}
                  onChange={(e) => updateFaq(faq.id, "question", e.target.value)}
                />
                <Textarea
                  placeholder="Answer"
                  value={faq.answer}
                  onChange={(e) => updateFaq(faq.id, "answer", e.target.value)}
                  rows={3}
                />
                {subProperties.length > 0 && (
                  <p className="text-xs text-muted-foreground">
                    Scope: <span className="font-medium">{scopeLabel(faq)}</span>
                  </p>
                )}
              </div>
              <Button variant="ghost" size="icon" onClick={() => removeFaq(faq.id)}>
                <Trash2 className="h-4 w-4 text-destructive" />
              </Button>
            </div>
          </CardContent>
        </Card>
      ))}

      {visibleFaqs.length === 0 && !uploading && (
        <div className="rounded-md border border-dashed py-10 text-center text-sm text-muted-foreground">
          No FAQ entries{scopeValue && scopeValue !== "shared" ? " for this property" : scopeValue === "shared" ? " shared across properties" : ""} yet. Add one manually or upload a CSV file.
        </div>
      )}

      <Button onClick={handleSave} disabled={saving || faqs.length === 0}>
        {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Save className="mr-2 h-4 w-4" />}
        Save FAQ
      </Button>
    </div>
  );
};

export default FaqToolsTab;
