import { useCallback, useEffect, useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Loader2, RotateCcw } from "lucide-react";
import { cn } from "@/lib/utils";
import { useToast } from "@/hooks/use-toast";
import FormEditor from "@/components/forms/FormEditor";
import type { BotOption, FieldTypeOption, LeadForm } from "@/components/forms/types";

/** Stable keys for list rendering; stripped again before saving. */
const withKeys = (form: LeadForm): LeadForm => ({
  ...form,
  fields: form.fields.map((f, i) => ({
    ...f,
    _key: f._key ?? `f_${i}_${Math.random().toString(36).slice(2, 7)}`,
  })),
});

const FormsDesign = () => {
  const { toast } = useToast();

  const [bots, setBots] = useState<BotOption[]>([]);
  const [fieldTypes, setFieldTypes] = useState<FieldTypeOption[]>([]);
  const [selected, setSelected] = useState<string | null>(null);

  const [form, setForm] = useState<LeadForm | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);

  // Bots + field types are static for the session; load once.
  useEffect(() => {
    fetch("/admin/api/forms/bots")
      .then((r) => r.json())
      .then((d) => {
        const list: BotOption[] = d.bots ?? [];
        setBots(list);
        setSelected((prev) => prev ?? list[0]?.hotel_code ?? null);
      })
      .catch(() =>
        toast({ title: "Could not load bots", variant: "destructive" }),
      );

    fetch("/admin/api/forms/field-types")
      .then((r) => r.json())
      .then((d) => setFieldTypes(d.field_types ?? []))
      .catch(() => setFieldTypes([]));
  }, [toast]);

  const loadForm = useCallback(
    (hotelCode: string) => {
      setLoading(true);
      fetch(`/admin/api/forms?hotel_code=${encodeURIComponent(hotelCode)}`)
        .then((r) => r.json())
        .then((d) => {
          setForm(withKeys(d.form));
          setDirty(false);
        })
        .catch(() =>
          toast({ title: "Could not load this form", variant: "destructive" }),
        )
        .finally(() => setLoading(false));
    },
    [toast],
  );

  useEffect(() => {
    if (selected) loadForm(selected);
  }, [selected, loadForm]);

  const updateForm = (updates: Partial<LeadForm>) => {
    setForm((prev) => (prev ? { ...prev, ...updates } : prev));
    setDirty(true);
  };

  const selectBot = (hotelCode: string) => {
    if (hotelCode === selected) return;
    if (dirty && !window.confirm("Discard unsaved changes to this form?")) return;
    setSelected(hotelCode);
  };

  const save = async () => {
    if (!form || !selected) return;
    setSaving(true);
    try {
      const response = await fetch("/admin/api/forms", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          hotel_code: selected,
          title: form.title,
          description: form.description,
          submit_label: form.submit_label,
          success_message: form.success_message,
          consent: form.consent,
          is_active: form.is_active,
          auto_load_form: form.auto_load_form,
          // _key is client-only bookkeeping.
          fields: form.fields.map(({ _key, ...rest }) => rest),
          otp_config: form.otp_config,
        }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        // The server rejects impossible configs (OTP with no matching field);
        // surface its wording rather than a generic failure.
        toast({
          title: "Not saved",
          description: body.detail ?? (await response.text()),
          variant: "destructive",
        });
        return;
      }
      setForm(withKeys(body.form));
      setDirty(false);
      toast({ title: `Saved — ${body.form.action ?? "updated"}` });
    } catch (err) {
      toast({
        title: "Save failed",
        description: String(err),
        variant: "destructive",
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex-1 overflow-auto p-6 min-h-0">
      <div className="mx-auto max-w-5xl space-y-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-2xl font-bold">Forms</h1>
            <p className="text-muted-foreground">
              The details form each bot shows when a new chat starts
            </p>
          </div>
          <div className="flex items-center gap-2">
            {dirty ? (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => selected && loadForm(selected)}
                disabled={saving}
              >
                <RotateCcw className="mr-2 h-3.5 w-3.5" /> Discard
              </Button>
            ) : null}
            <Button onClick={save} disabled={!form || saving || !dirty}>
              {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
              {dirty ? "Save changes" : "Saved"}
            </Button>
          </div>
        </div>

        <div className="grid gap-6 lg:grid-cols-[240px_1fr]">
          <div className="space-y-2">
            <p className="px-1 text-xs font-medium uppercase text-muted-foreground">
              Bot
            </p>
            {bots.length === 0
              ? [0, 1, 2].map((i) => <Skeleton key={i} className="h-14 w-full" />)
              : bots.map((bot) => (
                  <button
                    key={bot.hotel_code}
                    onClick={() => selectBot(bot.hotel_code)}
                    className={cn(
                      "w-full rounded-lg border p-3 text-left transition-colors hover:bg-accent/50",
                      selected === bot.hotel_code && "border-primary bg-accent/50",
                    )}
                  >
                    <div className="truncate text-sm font-medium">{bot.name}</div>
                    <Badge
                      variant="secondary"
                      className="mt-1 max-w-full truncate font-mono text-[10px]"
                    >
                      {bot.hotel_code}
                    </Badge>
                  </button>
                ))}
          </div>

          <div>
            {loading || !form ? (
              <Card>
                <CardContent className="space-y-3 p-6">
                  <Skeleton className="h-6 w-56" />
                  <Skeleton className="h-24 w-full" />
                  <Skeleton className="h-24 w-full" />
                </CardContent>
              </Card>
            ) : (
              <FormEditor
                form={form}
                fieldTypes={fieldTypes}
                onUpdate={updateForm}
              />
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

export default FormsDesign;
