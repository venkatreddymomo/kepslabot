import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { AlertTriangle, ShieldCheck } from "lucide-react";
import type { FormField, OtpChannel, OtpConfig } from "./types";

interface Props {
  otp: OtpConfig;
  fields: FormField[];
  onChange: (updates: Partial<OtpConfig>) => void;
}

const CHANNELS: { value: OtpChannel; label: string; hint: string }[] = [
  { value: "phone", label: "Phone", hint: "Code goes to the guest's mobile number." },
  { value: "email", label: "Email", hint: "Code goes to the guest's email address." },
  {
    value: "both",
    label: "Guest chooses",
    hint: "The guest picks where the single code is sent.",
  },
];

const OtpSettings = ({ otp, fields, onChange }: Props) => {
  const hasPhone = fields.some((f) => f.fieldType === "MOBILE_NUMBER");
  const hasEmail = fields.some((f) => f.fieldType === "EMAIL");

  // Mirrors the server-side guard, so the operator sees the problem while
  // editing instead of getting a 400 on save.
  const needsPhone = otp.channel === "phone" || otp.channel === "both";
  const needsEmail = otp.channel === "email" || otp.channel === "both";
  const missing: string[] = [];
  if (otp.enabled && needsPhone && !hasPhone) missing.push("a Phone number field");
  if (otp.enabled && needsEmail && !hasEmail) missing.push("an Email field");

  const num = (value: string, fallback: number) => {
    const n = Number.parseInt(value, 10);
    return Number.isFinite(n) ? n : fallback;
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between rounded-lg border p-3">
        <div className="flex items-start gap-2">
          <ShieldCheck className="mt-0.5 h-4 w-4 text-muted-foreground" />
          <div>
            <Label className="text-sm font-medium">Verify the guest with an OTP</Label>
            <p className="text-[11px] text-muted-foreground">
              After the form is submitted, ask for a one-time code before chatting.
            </p>
          </div>
        </div>
        <Switch
          checked={otp.enabled}
          onCheckedChange={(v) => onChange({ enabled: v })}
        />
      </div>

      {otp.enabled ? (
        <>
          {missing.length > 0 ? (
            <Alert variant="destructive">
              <AlertTriangle className="h-4 w-4" />
              <AlertDescription>
                This form needs {missing.join(" and ")} before it can be saved.
              </AlertDescription>
            </Alert>
          ) : null}

          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label className="text-xs">Send the code to</Label>
              <Select
                value={otp.channel}
                onValueChange={(v) => onChange({ channel: v as OtpChannel })}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {CHANNELS.map((c) => (
                    <SelectItem key={c.value} value={c.value}>
                      {c.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p className="text-[11px] text-muted-foreground">
                {CHANNELS.find((c) => c.value === otp.channel)?.hint}
              </p>
            </div>

            <div className="space-y-1.5">
              <Label className="text-xs">Code length (4–8)</Label>
              <Input
                type="number"
                min={4}
                max={8}
                value={otp.otpLength}
                onChange={(e) => onChange({ otpLength: num(e.target.value, 6) })}
              />
            </div>

            <div className="space-y-1.5">
              <Label className="text-xs">Code valid for (seconds, 30–1800)</Label>
              <Input
                type="number"
                min={30}
                max={1800}
                value={otp.otpValiditySeconds}
                onChange={(e) =>
                  onChange({ otpValiditySeconds: num(e.target.value, 300) })
                }
              />
            </div>

            <div className="space-y-1.5">
              <Label className="text-xs">Max resends (0–10)</Label>
              <Input
                type="number"
                min={0}
                max={10}
                value={otp.maxResendAttempts}
                onChange={(e) =>
                  onChange({ maxResendAttempts: num(e.target.value, 3) })
                }
              />
            </div>

            <div className="space-y-1.5 sm:col-span-2">
              <Label className="text-xs">Wait between resends (seconds, 0–600)</Label>
              <Input
                type="number"
                min={0}
                max={600}
                value={otp.resendCooldownSeconds}
                onChange={(e) =>
                  onChange({ resendCooldownSeconds: num(e.target.value, 60) })
                }
              />
            </div>
          </div>

          <div className="space-y-2 rounded-lg border p-3">
            <div className="flex items-center justify-between">
              <div>
                <Label className="text-sm">Let the guest skip verification</Label>
                <p className="text-[11px] text-muted-foreground">
                  Shows a Skip button. The lead is still captured, just unverified.
                </p>
              </div>
              <Switch
                checked={otp.allowSkip}
                onCheckedChange={(v) => onChange({ allowSkip: v })}
              />
            </div>
            <div className="flex items-center justify-between border-t pt-2">
              <div>
                <Label className="text-sm">Let the guest change their number</Label>
                <p className="text-[11px] text-muted-foreground">
                  Shows a Change number button on the code screen.
                </p>
              </div>
              <Switch
                checked={otp.allowChangeNumber}
                onCheckedChange={(v) => onChange({ allowChangeNumber: v })}
              />
            </div>
            <div className="flex items-center justify-between border-t pt-2">
              <div>
                <Label className="text-sm">Show the resend countdown</Label>
                <p className="text-[11px] text-muted-foreground">
                  Counts down to when Resend becomes available again.
                </p>
              </div>
              <Switch
                checked={otp.showResendTimer}
                onCheckedChange={(v) => onChange({ showResendTimer: v })}
              />
            </div>
          </div>
        </>
      ) : null}
    </div>
  );
};

export default OtpSettings;
