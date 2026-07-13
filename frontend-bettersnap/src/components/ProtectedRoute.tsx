import { Navigate } from "react-router-dom";
import { useIsAuthenticated, useMsal } from "@azure/msal-react";
import { InteractionStatus } from "@azure/msal-browser";
import { Loader2 } from "lucide-react";

const ProtectedRoute = ({ children }: { children: React.ReactNode }) => {
  const isAuthenticated = useIsAuthenticated();
  const { inProgress } = useMsal();

  const isLoading = inProgress !== InteractionStatus.None;

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background">
        <Loader2 className="w-8 h-8 text-primary animate-spin" />
      </div>
    );
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  return <>{children}</>;
};

export default ProtectedRoute;
