import { describe, expect, it } from "vitest";
import {
  PHONE_COUNTRIES,
  formatPhonePreview,
  splitPhone,
  toE164,
  toPhoneCountry,
  validatePhone,
} from "./phone";

describe("phone countries", () => {
  it("lists every region, not a hardcoded ten", () => {
    expect(PHONE_COUNTRIES.length).toBeGreaterThan(240);
    const kenya = PHONE_COUNTRIES.find((c) => c.iso === "KE");
    expect(kenya).toMatchObject({ dial: "+254", name: "Kenya", flag: "🇰🇪" });
  });

  it("maps ISO codes and legacy dial codes to a region", () => {
    expect(toPhoneCountry("IN")).toBe("IN");
    expect(toPhoneCountry("ca")).toBe("CA");
    expect(toPhoneCountry("+91")).toBe("IN");
    expect(toPhoneCountry("+1")).toBe("US");
    expect(toPhoneCountry("+44")).toBe("GB");
    expect(toPhoneCountry("")).toBe("IN");
    expect(toPhoneCountry("+99999")).toBe("IN");
  });
});

describe("validatePhone", () => {
  it("accepts real numbers the old table rejected or could not reach", () => {
    expect(validatePhone("2079460958", "GB")).toBeNull(); // old rule: +44 min 10
    expect(validatePhone("9812345678", "NP")).toBeNull(); // Nepal wasn't selectable
    expect(validatePhone("712345678", "KE")).toBeNull();
    expect(validatePhone("9845061230", "IN")).toBeNull();
    expect(validatePhone("09845061230", "IN")).toBeNull(); // trunk 0
    expect(validatePhone("4165550123", "US")).toBeNull(); // Canadian, shares +1
  });

  it("rejects bad numbers with a country-specific message", () => {
    expect(validatePhone("", "IN")).toBe("Phone number is required");
    expect(validatePhone("98450", "IN")).toMatch(/^Too short for India \(\+91\)/);
    expect(validatePhone("5845061230", "IN")).toMatch(/^Enter a valid India \(\+91\) number/);
    expect(validatePhone("98450612301", "IN")).not.toBeNull();
  });

  it("still rejects obviously fake numbers the library calls valid", () => {
    expect(validatePhone("9999999999", "IN")).toBe("Please enter a real phone number");
  });
});

describe("toE164 / splitPhone / formatPhonePreview", () => {
  it("submits E.164", () => {
    expect(toE164("9845061230", "IN")).toBe("+919845061230");
    expect(toE164("09845061230", "IN")).toBe("+919845061230");
    expect(toE164("4165550123", "US")).toBe("+14165550123");
  });

  it("splits prefilled values into country + national digits", () => {
    expect(splitPhone("+91 9845061230")).toEqual({ country: "IN", digits: "9845061230" });
    expect(splitPhone("+14165550123")).toEqual({ country: "CA", digits: "4165550123" });
    expect(splitPhone("+9779812345678")).toEqual({ country: "NP", digits: "9812345678" });
    expect(splitPhone("918858475656")).toEqual({ country: "IN", digits: "8858475656" });
    expect(splitPhone("98450 61230")).toEqual({ country: "IN", digits: "9845061230" });
    expect(splitPhone("")).toEqual({ country: "IN", digits: "" });
    expect(splitPhone("+91")).toEqual({ country: "IN", digits: "" });
  });

  it("previews the number in international format", () => {
    expect(formatPhonePreview("9845061230", "IN")).toBe("+91 98450 61230");
    expect(formatPhonePreview("", "IN")).toBe("");
  });
});
