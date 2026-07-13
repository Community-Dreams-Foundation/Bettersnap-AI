import { useState } from "react";
import { Link, Navigate, useNavigate, useLocation } from "react-router-dom";
import { useMsal, useIsAuthenticated } from "@azure/msal-react";
import { InteractionStatus } from "@azure/msal-browser";
import { loginRequest } from "@/lib/authConfig";
import PageShell from "@/components/PageShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Camera, Loader2, ArrowRight, ArrowLeft } from "lucide-react";
import { GoogleLogo } from "@/components/GoogleLogo";
import { toast } from "sonner";

const Login = () => {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();
  const { instance, inProgress } = useMsal();
  const isAuthenticated = useIsAuthenticated();

  if (inProgress === InteractionStatus.None && isAuthenticated) {
    return <Navigate to="/dashboard" replace />;
  }

  const fromOrgOnboarding = (location.state as any)?.fromOrgOnboarding === true;
  const teamName = (location.state as any)?.teamName as string | undefined;

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    try {
      await instance.loginRedirect({
        ...loginRequest,
        loginHint: email,
      });
    } catch (error: any) {
      if (error.errorCode !== "user_cancelled") {
        toast.error("Login failed", { description: error.message });
      }
      setLoading(false);
    }
  };

  const handleGoogleLogin = async () => {
    setLoading(true);
    try {
      await instance.loginRedirect({
        ...loginRequest,
        extraQueryParameters: { domain_hint: "google.com" },
      });
    } catch (error: any) {
      if (error.errorCode !== "user_cancelled") {
        toast.error("Google login failed", { description: error.message });
      }
      setLoading(false);
    }
  };

  return (
    <PageShell>
      <div className="min-h-screen flex items-center justify-center px-4">
        <div className="glass rounded-2xl shadow-glass p-8 w-full max-w-md space-y-6 relative">
          <Link
            to="/"
            className="absolute top-4 left-4 p-2 rounded-lg glass border-border text-muted-foreground hover:text-foreground transition-colors"
          >
            <ArrowLeft className="w-5 h-5" />
          </Link>
          <div className="text-center space-y-2 pt-4">
            <div className="w-12 h-12 rounded-xl gradient-cta flex items-center justify-center mx-auto">
              <Camera className="w-6 h-6 text-primary-foreground" />
            </div>
            <h1 className="text-2xl font-heading font-bold text-foreground">
              {fromOrgOnboarding ? "Sign in to your admin account" : "Welcome back"}
            </h1>
            <p className="text-muted-foreground text-sm">
              {fromOrgOnboarding && teamName ? `Setting up "${teamName}"` : "Sign in to your BetterSnap AI account"}
            </p>
          </div>

          <form onSubmit={handleLogin} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="email" className="text-foreground">
                Email
              </Label>
              <Input
                id="email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
                required
                className="glass border-border"
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="password" className="text-foreground">
                Password
              </Label>
              <Input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                required
                className="glass border-border"
              />
            </div>
            <Button type="submit" disabled={loading} className="w-full gradient-cta text-primary-foreground btn-glow">
              {loading ? <Loader2 className="w-4 h-4 animate-spin mr-2" /> : null}
              Sign In <ArrowRight className="w-4 h-4 ml-1" />
            </Button>
          </form>

          <div className="relative">
            <div className="absolute inset-0 flex items-center">
              <span className="w-full border-t border-border" />
            </div>
            <div className="relative flex justify-center text-xs uppercase">
              <span className="bg-background px-2 text-muted-foreground">Or continue with</span>
            </div>
          </div>

          <div className="grid grid-cols-1 gap-3">
            <Button
              type="button"
              variant="outline"
              onClick={handleGoogleLogin}
              disabled={loading}
              className="w-full bg-white border border-gray-300 text-gray-700 hover:bg-gray-50 hover:text-gray-900"
            >
              {loading ? <Loader2 className="w-4 h-4 animate-spin mr-2" /> : <GoogleLogo className="w-4 h-4 mr-2" />}
              Google
            </Button>
          </div>

          <p className="text-center text-xs text-muted-foreground">
            By continuing, you agree to our{" "}
            <Link to="/privacy-policy" className="text-primary hover:underline">
              Privacy Policy
            </Link>
            .
          </p>

          <p className="text-center text-sm text-muted-foreground">
            Don't have an account?{" "}
            <Link
              to="/signup"
              state={fromOrgOnboarding ? { fromOrgOnboarding: true, teamName } : undefined}
              className="text-primary hover:underline font-medium"
            >
              Sign up
            </Link>
          </p>
        </div>
      </div>
    </PageShell>
  );
};

export default Login;
