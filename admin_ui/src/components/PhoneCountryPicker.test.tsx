import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { PhoneCountryPicker } from "./PhoneCountryPicker";

describe("PhoneCountryPicker", () => {
  it("shows the selected flag + dial code", () => {
    render(<PhoneCountryPicker value="IN" onChange={() => {}} />);
    expect(screen.getByRole("button", { name: "Country code" })).toHaveTextContent("🇮🇳+91");
  });

  it("finds a country by name and picks it", () => {
    const onChange = vi.fn();
    render(<PhoneCountryPicker value="IN" onChange={onChange} />);
    fireEvent.click(screen.getByRole("button", { name: "Country code" }));
    fireEvent.change(screen.getByPlaceholderText("Search country or code"), { target: { value: "kenya" } });
    fireEvent.click(screen.getByRole("button", { name: /Kenya/ }));
    expect(onChange).toHaveBeenCalledWith("KE");
    expect(screen.queryByPlaceholderText("Search country or code")).toBeNull();
  });

  it("finds a country by dial code, and Enter picks without submitting the form", () => {
    const onChange = vi.fn();
    const onSubmit = vi.fn((e: { preventDefault: () => void }) => e.preventDefault());
    render(
      <form onSubmit={onSubmit}>
        <PhoneCountryPicker value="IN" onChange={onChange} />
      </form>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Country code" }));
    const search = screen.getByPlaceholderText("Search country or code");
    fireEvent.change(search, { target: { value: "+977" } });
    fireEvent.keyDown(search, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith("NP");
    expect(onSubmit).not.toHaveBeenCalled();
  });
});
