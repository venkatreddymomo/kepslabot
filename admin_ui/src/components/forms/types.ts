// Shapes mirror /admin/api/forms exactly. The server is the authority on
// defaults and clamping; the editor only sends what the operator typed.

export type FieldTypeValue =
  | "FULL_NAME"
  | "EMAIL"
  | "MOBILE_NUMBER"
  | "ALTERNATE_MOBILE_NUMBER"
  | "CITY"
  | "CUSTOM";

export interface FieldTypeOption {
  value: FieldTypeValue;
  label: string;
  /** Input type this semantic type implies (text / email / number). */
  type: string;
}

export interface FormField {
  label: string;
  type?: string;
  fieldType: FieldTypeValue;
  required?: boolean;
  placeholder?: string;
  validation?: Record<string, unknown>;
  textValidationType?: string;
  numberValidationType?: string;
  options?: string[];
  /** Client-only key for stable list rendering; never sent to the server. */
  _key?: string;
}

export type OtpChannel = "phone" | "email" | "both";

export interface OtpConfig {
  enabled: boolean;
  channel: OtpChannel;
  allowSkip: boolean;
  otpLength: number;
  showResendTimer: boolean;
  allowChangeNumber: boolean;
  maxResendAttempts: number;
  otpValiditySeconds: number;
  resendCooldownSeconds: number;
  otpValidationFormId?: string;
}

export interface LeadForm {
  hotel_code: string;
  form_key: string;
  /** false = never saved; the editor is showing built-in defaults. */
  exists: boolean;
  id?: number;
  bot_id?: number;
  title: string;
  description: string;
  submit_label: string;
  success_message: string;
  /** Small print under the form; empty means no consent line is rendered. */
  consent: string;
  is_active: boolean;
  auto_load_form: boolean;
  fields: FormField[];
  otp_config: OtpConfig;
}

export interface BotOption {
  id: number;
  hotel_code: string;
  name: string;
}
