import { lazy, Suspense } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Toaster as Sonner } from "@/components/ui/sonner";
import { Toaster } from "@/components/ui/toaster";
import { TooltipProvider } from "@/components/ui/tooltip";
// AppLayout and NotFound are small and always needed — keep eager
import AppLayout from "./components/AppLayout";
import NotFound from "./pages/NotFound";

// All page-level components are lazy-loaded so each route only downloads
// its own chunk when the user first navigates to it.
const ProductDetail    = lazy(() => import("./pages/ProductDetail"));
const Dashboard        = lazy(() => import("./pages/app/Dashboard"));
const WebCrawl         = lazy(() => import("./pages/app/WebCrawl"));
const ContentManager   = lazy(() => import("./pages/app/ContentManager"));
const BotTraining      = lazy(() => import("./pages/app/BotTraining"));
const FormsDesign      = lazy(() => import("./pages/app/FormsDesign"));
const StaffManagement  = lazy(() => import("./pages/app/StaffManagement"));
const NotificationRules = lazy(() => import("./pages/app/NotificationRules"));
const EscalationMatrix = lazy(() => import("./pages/app/EscalationMatrix"));
const Departments      = lazy(() => import("./pages/app/Departments"));
const Widget           = lazy(() => import("./pages/app/Widget"));
const GuestChatHistory = lazy(() => import("./pages/app/GuestChatHistory"));
const TicketList       = lazy(() => import("./pages/app/TicketList"));

const PageLoader = () => (
  <div className="flex h-full w-full items-center justify-center py-24">
    <div className="h-7 w-7 animate-spin rounded-full border-2 border-muted border-t-foreground" />
  </div>
);

const queryClient = new QueryClient();

const App = () => (
  <QueryClientProvider client={queryClient}>
    <TooltipProvider>
      <Toaster />
      <Sonner />
      <BrowserRouter
        basename="/admin"
        future={{
          v7_relativeSplatPath: true,
          v7_startTransition: true,
        }}
      >
        <Suspense fallback={<PageLoader />}>
          <Routes>
            <Route path="/" element={<Navigate to="/app/dashboard" replace />} />
            <Route path="/products/neor-bot" element={<ProductDetail />} />
            <Route path="/app" element={<AppLayout />}>
              <Route index element={<Navigate to="dashboard" replace />} />
              <Route path="dashboard" element={<Dashboard />} />
              <Route path="crawl" element={<WebCrawl />} />
              <Route path="content" element={<ContentManager />} />
              <Route path="training" element={<BotTraining />} />
              <Route path="forms" element={<FormsDesign />} />
              <Route path="staff" element={<StaffManagement />} />
              <Route path="departments" element={<Departments />} />
              <Route path="notifications" element={<NotificationRules />} />
              <Route path="escalation" element={<EscalationMatrix />} />
              <Route path="widget" element={<Widget />} />
              <Route path="chat-history" element={<GuestChatHistory />} />
              <Route path="tickets" element={<TicketList />} />
            </Route>
            <Route path="*" element={<NotFound />} />
          </Routes>
        </Suspense>
      </BrowserRouter>
    </TooltipProvider>
  </QueryClientProvider>
);

export default App;
