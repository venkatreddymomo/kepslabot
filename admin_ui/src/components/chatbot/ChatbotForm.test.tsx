import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { ChatbotForm } from "./ChatbotForm";
import type { FormField } from "./types";

const phoneField: FormField[] = [{ name: "phone", label: "Phone", type: "tel", required: true }];

const renderForm = (onSubmit = vi.fn()) => {
  render(<ChatbotForm fields={phoneField} onSubmit={onSubmit} brandColor="#0ea5e9" />);
  return {
    onSubmit,
    country: screen.getByRole("button", { name: "Country code" }),
    input: screen.getByRole("textbox") as HTMLInputElement,
    submit: screen.getByRole("button", { name: "Submit" }),
  };
};

const pickCountry = (country: HTMLElement, query: string) => {
  fireEvent.click(country);
  fireEvent.change(screen.getByPlaceholderText("Search country or code"), { target: { value: query } });
  fireEvent.keyDown(screen.getByPlaceholderText("Search country or code"), { key: "Enter" });
};

describe("ChatbotForm phone field", () => {
  it("submits the number in E.164 for the chosen country", () => {
    const { onSubmit, country, input, submit } = renderForm();
    pickCountry(country, "nepal");
    fireEvent.change(input, { target: { value: "9812345678" } });
    fireEvent.click(submit);
    expect(onSubmit).toHaveBeenCalledWith({ phone: "+9779812345678" });
  });

  it("blocks submit and names the country when the number is wrong for it", () => {
    const { onSubmit, country, input, submit } = renderForm();
    pickCountry(country, "nepal");
    fireEvent.change(input, { target: { value: "123" } });
    fireEvent.click(submit);
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByText(/Too short for Nepal \(\+977\)/)).toBeInTheDocument();
  });

  it("keeps the typed digits when the country changes", () => {
    const { country, input } = renderForm();
    fireEvent.change(input, { target: { value: "9845061230" } });
    pickCountry(country, "kenya");
    expect(input.value).toBe("9845061230");
  });

  it("accepts a UK number the old 10-digit rule rejected", () => {
    const { onSubmit, country, input, submit } = renderForm();
    pickCountry(country, "united kingdom");
    fireEvent.change(input, { target: { value: "7400123456" } });
    fireEvent.click(submit);
    expect(onSubmit).toHaveBeenCalledWith({ phone: "+447400123456" });
  });
});
