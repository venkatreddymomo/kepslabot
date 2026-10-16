import { defineConfig } from "vite";
import react from "@vitejs/plugin-react-swc";
import path from "path";
import { componentTagger } from "lovable-tagger";

const backendPort = process.env.KEPSLA_BACKEND_PORT || "8011";
const backendTarget = `http://localhost:${backendPort}`;
const backendWsTarget = `ws://localhost:${backendPort}`;

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => ({
  base: "/admin/",
  build: {
    rollupOptions: {
      input: {
        main: path.resolve(__dirname, "index.html"),
        chat: path.resolve(__dirname, "chat.html"),
      },
      output: {
        manualChunks: {
          // Core React runtime — always needed on every page
          "vendor-react": ["react", "react-dom", "react-router-dom"],
          // Server state management
          "vendor-query": ["@tanstack/react-query"],
          // All Radix UI primitives — shared across every admin page
          "vendor-radix": [
            "@radix-ui/react-accordion",
            "@radix-ui/react-alert-dialog",
            "@radix-ui/react-aspect-ratio",
            "@radix-ui/react-avatar",
            "@radix-ui/react-checkbox",
            "@radix-ui/react-collapsible",
            "@radix-ui/react-context-menu",
            "@radix-ui/react-dialog",
            "@radix-ui/react-dropdown-menu",
            "@radix-ui/react-hover-card",
            "@radix-ui/react-label",
            "@radix-ui/react-menubar",
            "@radix-ui/react-navigation-menu",
            "@radix-ui/react-popover",
            "@radix-ui/react-progress",
            "@radix-ui/react-radio-group",
            "@radix-ui/react-scroll-area",
            "@radix-ui/react-select",
            "@radix-ui/react-separator",
            "@radix-ui/react-slider",
            "@radix-ui/react-slot",
            "@radix-ui/react-switch",
            "@radix-ui/react-tabs",
            "@radix-ui/react-toast",
            "@radix-ui/react-toggle",
            "@radix-ui/react-toggle-group",
            "@radix-ui/react-tooltip",
          ],
          // Styling utilities
          "vendor-ui-utils": [
            "class-variance-authority",
            "clsx",
            "tailwind-merge",
            "lucide-react",
            "sonner",
            "cmdk",
          ],
          // Form handling — only admin pages with forms need this
          "vendor-forms": ["react-hook-form", "@hookform/resolvers", "zod"],
          // Charts — only Dashboard uses recharts
          "vendor-charts": ["recharts"],
          // Animation — framer-motion is large, isolate it
          "vendor-animation": ["framer-motion"],
          // Date utilities
          "vendor-date": ["date-fns", "react-day-picker"],
          // Misc UI
          "vendor-misc": [
            "embla-carousel-react",
            "input-otp",
            "next-themes",
            "react-resizable-panels",
            "vaul",
          ],
        },
      },
    },
  },
  server: {
    host: "::",
    port: 8080,
    hmr: {
      overlay: false,
      port: 8080,
      host: "localhost",
    },
    proxy: {
      // KB scraper API goes direct to port 8501 — no KePSLA hop needed
      "/kb-scraper/api": {
        target: "http://localhost:8501",
        changeOrigin: true,
        rewrite: (path: string) => path.replace(/^\/kb-scraper/, ""),
      },
      // KePSLA admin & chat APIs
      "/admin/api": { target: backendTarget, changeOrigin: true },
      "/api": { target: backendTarget, changeOrigin: true },
      // Widget loader and static assets served by FastAPI
      "/static": { target: backendTarget, changeOrigin: true },
      // Chat WebSocket transport — proxy with ws upgrade so the widget can
      // connect same-origin during local dev.
      "/ws": { target: backendWsTarget, ws: true, changeOrigin: true },
    },
  },
  plugins: [react(), mode === "development" && componentTagger()].filter(Boolean),
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
    dedupe: ["react", "react-dom", "react/jsx-runtime", "react/jsx-dev-runtime"],
  },
}));
