// Phone numbers for every form phone field, backed by libphonenumber-js — the
// same Google libphonenumber metadata the backend's `phonenumbers` check uses,
// so the widget and the server agree on what a valid number is.
//
// Country state is an ISO region ("IN", "US", "CA"), never a dial code: +1 is
// shared by the US, Canada and the Caribbean, so "+1" alone can't say which
// numbering plan to validate against.
import {
  Metadata,
  getCountries,
  getCountryCallingCode,
  parsePhoneNumberFromString,
  validatePhoneNumberLength,
  type CountryCode,
} from "libphonenumber-js/max";

export type PhoneCountryCode = CountryCode;

export interface PhoneCountry {
  iso: CountryCode;
  dial: string;
  name: string;
  flag: string;
}

export const DEFAULT_PHONE_COUNTRY: CountryCode = "IN";

// E.164 caps a whole number at 15 digits, so no national number is longer.
export const PHONE_MAX_DIGITS = 15;

let regionNames: Intl.DisplayNames | null = null;
try {
  regionNames = new Intl.DisplayNames(["en"], { type: "region" });
} catch {
  regionNames = null;
}

function countryName(iso: string): string {
  try {
    return regionNames?.of(iso) || iso;
  } catch {
    return iso;
  }
}

// Emoji flag from the two regional-indicator letters — no image requests.
function countryFlag(iso: string): string {
  return String.fromCodePoint(...[...iso.toUpperCase()].map((ch) => 0x1f1e6 + ch.charCodeAt(0) - 65));
}

export const PHONE_COUNTRIES: PhoneCountry[] = getCountries()
  .map((iso) => ({
    iso,
    dial: `+${getCountryCallingCode(iso)}`,
    name: countryName(iso),
    flag: countryFlag(iso),
  }))
  .sort((a, b) => a.name.localeCompare(b.name));

const COUNTRY_BY_ISO = new Map<string, PhoneCountry>(PHONE_COUNTRIES.map((c) => [c.iso, c]));

// Dial code -> regions sharing it, main region first ("1" -> ["US", "AG", ...]).
const REGIONS_BY_DIAL = (
  new Metadata() as unknown as { countryCallingCodes(): Record<string, CountryCode[]> }
).countryCallingCodes();

// How many digits a country's numbering plan actually uses, read from the same
// metadata that validates the number — India 10, Singapore 8, Austria 13. The
// field caps itself from this, so "too long" is something the guest cannot type
// rather than something they are told about at submit.
//
// Mobile + landline lengths, NOT the plan's general lengths: India's general
// lengths run to 13 (special-service ranges nobody hands you as a contact
// number), which would put the cap back where the complaint started.
const DIALABLE_TYPES = ["MOBILE", "FIXED_LINE", "FIXED_LINE_OR_MOBILE"] as const;

interface NumberingPlanLike {
  type(kind: string): { possibleLengths(): number[] } | undefined;
  possibleLengths(): number[];
}

const MAX_DIGITS_BY_COUNTRY = new Map<string, number>();

/** Longest national number `country` allows — derived from the library's own
 * metadata, never a hardcoded table. */
export function phoneMaxDigits(country: CountryCode): number {
  const cached = MAX_DIGITS_BY_COUNTRY.get(country);
  if (cached) return cached;
  let longest = 0;
  try {
    const meta = new Metadata() as unknown as {
      selectNumberingPlan(iso: CountryCode): void;
      numberingPlan: NumberingPlanLike | undefined;
    };
    meta.selectNumberingPlan(country);
    const plan = meta.numberingPlan;
    for (const kind of DIALABLE_TYPES) {
      const lengths = plan?.type(kind)?.possibleLengths();
      if (lengths?.length) longest = Math.max(longest, ...lengths);
    }
    // A plan with no mobile/landline ranges of its own still has general ones.
    if (!longest) {
      const general = plan?.possibleLengths();
      if (general?.length) longest = Math.max(...general);
    }
  } catch {
    longest = 0;
  }
  const capped = Math.min(longest || PHONE_MAX_DIGITS, PHONE_MAX_DIGITS);
  MAX_DIGITS_BY_COUNTRY.set(country, capped);
  return capped;
}

/** What the field should hold after a keystroke or a paste: digits only, never
 * more than `country` allows. A guest who typed the trunk prefix ("0 98450…")
 * or pasted the number with its country code ("+91 98450…") has not overflowed
 * — the library knows neither belongs to the national number, so drop those
 * instead of truncating the real digits off the end. */
export function clampPhoneDigits(value: unknown, country: CountryCode): string {
  const digits = String(value ?? "").replace(/\D/g, "");
  const max = phoneMaxDigits(country);
  if (digits.length <= max) return digits;
  const national = parsePhoneNumberFromString(digits, country)?.nationalNumber;
  if (national && String(national).length <= max) return String(national);
  return digits.slice(0, max);
}

export function phoneCountry(iso: string): PhoneCountry {
  return COUNTRY_BY_ISO.get(iso) || (COUNTRY_BY_ISO.get(DEFAULT_PHONE_COUNTRY) as PhoneCountry);
}

/** Accepts an ISO region ("IN") or a legacy dial code ("+91") and returns the
 * ISO region. A shared dial code resolves to its main region (+1 -> US). */
export function toPhoneCountry(value: unknown, fallback: CountryCode = DEFAULT_PHONE_COUNTRY): CountryCode {
  const raw = String(value ?? "").trim();
  if (!raw) return fallback;
  const upper = raw.toUpperCase();
  if (COUNTRY_BY_ISO.has(upper)) return upper as CountryCode;
  const main = REGIONS_BY_DIAL[raw.replace(/\D/g, "")]?.[0];
  return main && COUNTRY_BY_ISO.has(main) ? main : fallback;
}

/** Split a stored/prefilled phone ("+91 98450 61230", "+14165550123",
 * "918858475656", "9845061230") into the picker's country + national digits. */
export function splitPhone(
  raw: unknown,
  fallback: CountryCode = DEFAULT_PHONE_COUNTRY,
): { country: CountryCode; digits: string } {
  const text = String(raw ?? "").trim();
  const allDigits = text.replace(/\D/g, "");
  if (text.startsWith("+")) {
    const parsed = parsePhoneNumberFromString(text);
    if (!parsed) return { country: toPhoneCountry(text, fallback), digits: "" };
    const country =
      parsed.country && COUNTRY_BY_ISO.has(parsed.country)
        ? parsed.country
        : toPhoneCountry(parsed.countryCallingCode, fallback);
    return { country, digits: String(parsed.nationalNumber) };
  }
  // A valid number parses to its bare national digits: a country code baked in
  // WITHOUT the '+' ("918858475656" for +91) and a trunk "0" are both dropped.
  const parsed = allDigits ? parsePhoneNumberFromString(allDigits, fallback) : undefined;
  if (parsed?.isValid()) {
    const country = parsed.country && COUNTRY_BY_ISO.has(parsed.country) ? parsed.country : fallback;
    return { country, digits: String(parsed.nationalNumber) };
  }
  return { country: fallback, digits: allDigits.slice(0, PHONE_MAX_DIGITS) };
}

export function isObviouslyFakePhone(digits: string): boolean {
  if (!digits) return false;
  // All same digit ("9999999999")
  if (/^(\d)\1+$/.test(digits)) return true;
  // Strict ascending sequence ("1234567890" or "0123456789...")
  let ascending = true;
  for (let i = 1; i < digits.length; i++) {
    if ((parseInt(digits[i], 10) - parseInt(digits[i - 1], 10) + 10) % 10 !== 1) {
      ascending = false;
      break;
    }
  }
  if (ascending) return true;
  // Strict descending sequence ("9876543210")
  let descending = true;
  for (let i = 1; i < digits.length; i++) {
    if ((parseInt(digits[i - 1], 10) - parseInt(digits[i], 10) + 10) % 10 !== 1) {
      descending = false;
      break;
    }
  }
  return descending;
}

/** Error message for a national number under `country`, or null when valid. */
export function validatePhone(digits: unknown, country: CountryCode): string | null {
  const national = String(digits ?? "").replace(/\D/g, "");
  if (!national) return "Phone number is required";
  const { name, dial } = phoneCountry(country);
  const where = `${name} (${dial})`;
  const lengthIssue = validatePhoneNumberLength(national, country);
  if (lengthIssue === "TOO_SHORT") return `Too short for ${where}`;
  if (lengthIssue === "TOO_LONG") return `Too long for ${where}`;
  const parsed = parsePhoneNumberFromString(national, country);
  if (!parsed?.isValid()) return `Enter a valid ${where} number`;
  if (isObviouslyFakePhone(String(parsed.nationalNumber))) return "Please enter a real phone number";
  return null;
}

/** "+919845061230" — the form of the number every submit sends. */
export function toE164(digits: unknown, country: CountryCode): string {
  const national = String(digits ?? "").replace(/\D/g, "");
  const parsed = parsePhoneNumberFromString(national, country);
  return parsed ? String(parsed.number) : `${phoneCountry(country).dial}${national}`;
}

/** Live helper under the field: "+91 98450 61230" (empty when nothing typed). */
export function formatPhonePreview(digits: unknown, country: CountryCode): string {
  const national = String(digits ?? "").replace(/\D/g, "");
  if (!national) return "";
  const parsed = parsePhoneNumberFromString(national, country);
  return parsed ? parsed.formatInternational() : `${phoneCountry(country).dial} ${national}`;
}
