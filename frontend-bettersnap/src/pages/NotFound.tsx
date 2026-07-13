import { useLocation, Link } from "react-router-dom";
import { useEffect } from "react";
import { Home } from "lucide-react";
import PageShell from "@/components/PageShell";

const NotFound = () => {
  const location = useLocation();

  useEffect(() => {
    console.error("404 Error: User attempted to access non-existent route:", location.pathname);
  }, [location.pathname]);

  return (
    <PageShell className="flex items-center justify-center">
      <div className="text-center glass rounded-3xl p-12 shadow-glass max-w-md mx-4">
        <h1 className="mb-2 text-7xl font-heading font-bold text-gradient">404</h1>
        <p className="mb-6 text-xl text-muted-foreground">Oops! Page not found</p>
        <Link
          to="/"
          className="inline-flex items-center gap-2 px-6 py-3 rounded-xl gradient-cta text-primary-foreground font-semibold hover-scale btn-glow"
        >
          <Home className="w-4 h-4" aria-hidden="true" />
          Return to Home
        </Link>
      </div>
    </PageShell>
  );
};

export default NotFound;
