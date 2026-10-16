import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Settings2 } from "lucide-react";
import FormFieldBuilder from "./FormFieldBuilder";
import OtpSettings from "./OtpSettings";
import type { FieldTypeOption, FormField, LeadForm, OtpConfig } from "./types";

interface Props {
  form: LeadForm;
  fieldTypes: FieldTypeOption[];
  onUpdate: (updates: Partial<LeadForm>) => void;
}

const FormEditor = ({ form, fieldTypes, onUpdate }: Props) => {
  const patchOtp = (updates: Partial<OtpConfig>) =>
    onUpdate({ otp_config: { ...form.otp_config, ...updates } });

  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-lg">
          <Settings2 className="h-5 w-5" /> Opening lead form
          <Badge variant="secondary" className="ml-1 font-mono text-[10px]">
            {form.form_key}
          </Badge>
          {!form.exists ? (
            <Badge variant="outline" className="text-[10px]">
              not saved yet
            </Badge>
          ) : null}
        </CardTitle>
        <p className="text-xs text-muted-foreground">
          This is the first thing a guest sees when a new chat starts. The chat box
          stays locked until they submit it.
        </p>
      </CardHeader>

      <CardContent>
        <Tabs defaultValue="fields">
          <TabsList className="mb-4">
            <TabsTrigger value="fields">Fields</TabsTrigger>
            <TabsTrigger value="copy">Wording</TabsTrigger>
            <TabsTrigger value="otp">
              OTP
              {form.otp_config.enabled ? (
                <span className="ml-1.5 h-1.5 w-1.5 rounded-full bg-primary" />
              ) : null}
            </TabsTrigger>
          </TabsList>

          <TabsContent value="fields" className="space-y-4">
            <FormFieldBuilder
              fields={form.fields}
              fieldTypes={fieldTypes}
              onFieldsChange={(fields: FormField[]) => onUpdate({ fields })}
            />
          </TabsContent>

          <TabsContent value="copy" className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label className="text-xs">Form heading</Label>
                <Input
                  value={form.title}
                  onChange={(e) => onUpdate({ title: e.target.value })}
                  maxLength={100}
                />
              </div>
              <div className="space-y-1.5">
                <Label className="text-xs">Submit button text</Label>
                <Input
                  value={form.submit_label}
                  onChange={(e) => onUpdate({ submit_label: e.target.value })}
                  maxLength={100}
                />
              </div>
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs">Text under the heading</Label>
              <Textarea
                value={form.description}
                onChange={(e) => onUpdate({ description: e.target.value })}
                className="min-h-[60px] resize-none text-sm"
                maxLength={1000}
              />
            </div>
            <div className="space-y-1.5">
              <Label className="text-xs">Message after submitting</Label>
              <Textarea
                value={form.success_message}
                onChange={(e) => onUpdate({ success_message: e.target.value })}
                className="min-h-[60px] resize-none text-sm"
                maxLength={100}
              />
              <p className="text-[11px] text-muted-foreground">
                Kept short on purpose — the field it is stored in holds 100
                characters.
              </p>
            </div>

            <div className="space-y-1.5">
              <Label className="text-xs">Consent line (small print)</Label>
              <Textarea
                value={form.consent}
                onChange={(e) => onUpdate({ consent: e.target.value })}
                className="min-h-[50px] resize-none text-sm"
                placeholder="e.g. By continuing, you agree to our Privacy Policy."
              />
              <p className="text-[11px] text-muted-foreground">
                Shown under the fields. Leave empty for no consent line.
              </p>
            </div>

            <div className="flex items-center justify-between rounded-lg border p-3">
              <div>
                <Label className="text-sm">Show this form automatically</Label>
                <p className="text-[11px] text-muted-foreground">
                  Off means the guest can start chatting without filling it in.
                </p>
              </div>
              <Switch
                checked={form.auto_load_form}
                onCheckedChange={(v) => onUpdate({ auto_load_form: v })}
              />
            </div>
          </TabsContent>

          <TabsContent value="otp">
            <OtpSettings
              otp={form.otp_config}
              fields={form.fields}
              onChange={patchOtp}
            />
          </TabsContent>
        </Tabs>
      </CardContent>
    </Card>
  );
};

export default FormEditor;
