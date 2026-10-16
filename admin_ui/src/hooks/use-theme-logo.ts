import { useState, useEffect } from "react";
import kepslaLogoLight from "@/assets/kepsla-logo.svg";
import kepslaLogoDark from "@/assets/kepsla-logo-dark.svg";

export const useThemeLogo = (): string => {
  const [isDark, setIsDark] = useState(() =>
    typeof window !== "undefined" && document.documentElement.classList.contains("dark")
  );

  useEffect(() => {
    const observer = new MutationObserver(() => {
      setIsDark(document.documentElement.classList.contains("dark"));
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);

  return isDark ? kepslaLogoDark : kepslaLogoLight;
};
