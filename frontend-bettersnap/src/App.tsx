import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Route, Routes, Navigate } from "react-router-dom";

import type { IPublicClientApplication } from "@azure/msal-browser";
import { InteractionStatus } from "@azure/msal-browser";
import { useIsAuthenticated, useMsal } from "@azure/msal-react";
import { Camera } from "lucide-react";
import { Toaster as Sonner } from "@/components/ui/sonner";
import { Toaster } from "@/components/ui/toaster";
import { TooltipProvider } from "@/components/ui/tooltip";
import { AuthProvider } from "@/contexts/AuthContext";
import ProtectedRoute from "@/components/ProtectedRoute";
import TermsModal from "@/components/TermsModal";
import Index from "./pages/Index";
import Login from "./pages/Login";
import UniversityLogin from "./pages/UniversityLogin";
import Signup from "./pages/Signup";
import UniversitySignup from "./pages/UniversitySignup";
import Onboarding from "./pages/Onboarding";
import Dashboard from "./pages/Dashboard";
import HeadshotGenerator from "./pages/HeadshotGenerator";
import History from "./pages/History";
import Settings from "./pages/Settings";
import Billing from "./pages/Billing";
import PrivacyPolicy from "./pages/PrivacyPolicy";
import Terms from "./pages/Terms";
import ContactSupport from "./pages/ContactSupport";
import UseCasePage from "./pages/UseCasePage";
import ForCompanies from "./pages/ForCompanies";
import OrgOnboarding from "./pages/OrgOnboarding";
import NotFound from "./pages/NotFound";

const queryClient = new QueryClient();

const AppContent = ({ msalInstance: _msalInstance }: { msalInstance: IPublicClientApplication }) => {
  const { inProgress } = useMsal();
  const isAuthenticated = useIsAuthenticated();

  // Show loading spinner while MSAL is initializing
  if (inProgress === InteractionStatus.Startup || inProgress === InteractionStatus.HandleRedirect) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background">
        <div className="flex flex-col items-center gap-3">
          <Camera className="w-8 h-8 text-primary animate-spin" />
          <p className="text-muted-foreground text-sm">Initializing...</p>
        </div>
      </div>
    );
  }

  return (
    <Routes>
      <Route path="/" element={<Index />} />
      <Route path="/login" element={<Login />} />
      <Route path="/university-login" element={<UniversityLogin />} />
      <Route path="/signup" element={<Signup />} />
      <Route path="/university-signup" element={<UniversitySignup />} />
      <Route
        path="/onboarding"
        element={
          <ProtectedRoute>
            <Onboarding />
          </ProtectedRoute>
        }
      />
      <Route
        path="/dashboard"
        element={
          <ProtectedRoute>
            <Dashboard />
          </ProtectedRoute>
        }
      />
      <Route
        path="/generate"
        element={
          <ProtectedRoute>
            <HeadshotGenerator />
          </ProtectedRoute>
        }
      />
      <Route
        path="/history"
        element={
          <ProtectedRoute>
            <History />
          </ProtectedRoute>
        }
      />
      <Route
        path="/settings"
        element={
          <ProtectedRoute>
            <Settings />
          </ProtectedRoute>
        }
      />
      <Route
        path="/billing"
        element={
          <ProtectedRoute>
            <Billing />
          </ProtectedRoute>
        }
      />
      <Route path="/privacy-policy" element={<PrivacyPolicy />} />
      <Route path="/terms" element={<Terms />} />
      <Route path="/contact-support" element={<ContactSupport />} />
      <Route path="/use-cases/:slug" element={<UseCasePage />} />
      <Route path="/for-companies" element={<ForCompanies />} />
      <Route path="/org/onboarding" element={<OrgOnboarding />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
};

const App = ({ msalInstance }: { msalInstance: IPublicClientApplication }) => (
  <>
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <Toaster />
        <Sonner />
        <BrowserRouter>
          <AuthProvider>
            <TermsModal />
            <AppContent msalInstance={msalInstance} />
          </AuthProvider>
        </BrowserRouter>
      </TooltipProvider>
    </QueryClientProvider>
  </>
);

export default App;
