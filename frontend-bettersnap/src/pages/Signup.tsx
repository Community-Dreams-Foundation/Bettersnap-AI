import { useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useMsal } from "@azure/msal-react";
import { loginRequest } from "@/lib/authConfig";
import PageShell from "@/components/PageShell";
import { Button } from "@/components/ui/button";
import { Camera, Loader2, ArrowRight, ArrowLeft } from "lucide-react";
import { GoogleLogo } from "@/components/GoogleLogo";
import { toast } from "sonner";

const Signup = () => {
  const [loading, setLoading] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();
  const { instance } = useMsal();

  const fromOrgOnboarding = (location.state as any)?.fromOrgOnboarding === true;
  const teamName = (location.state as any)?.teamName as string | undefined;

  const handleEmailSignup = async () => {
    setLoading(true);
    try {
      await instance.loginRedirect({
        ...loginRequest,
        prompt: "create",
      });
    } catch (error: any) {
      if (error.errorCode !== "user_cancelled") {
        toast.error("Signup failed", { description: error.message });
      }
      setLoading(false);
    }
  };

  const handleGoogleSignIn = async () => {
    setLoading(true);
    try {
      await instance.loginRedirect({
        ...loginRequest,
        extraQueryParameters: { domain_hint: "google.com" },
      });
    } catch (error: any) {
      if (error.errorCode !== "user_cancelled") {
        toast.error("Google sign in failed", { description: error.message });
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
              {fromOrgOnboarding ? "Create your admin account" : "Create account"}
            </h1>
            <p className="text-muted-foreground text-sm">
              {fromOrgOnboarding && teamName ? `Setting up "${teamName}"` : "Get started with BetterSnap AI for free"}
            </p>
          </div>

          <div className="space-y-4">
            <p className="text-center text-sm text-muted-foreground">Choose how you want to create your account</p>

            <Button
              type="button"
              onClick={handleEmailSignup}
              disabled={loading}
              className="w-full gradient-cta text-primary-foreground btn-glow"
            >
              {loading ? <Loader2 className="w-4 h-4 animate-spin mr-2" /> : null}
              Sign Up with Email <ArrowRight className="w-4 h-4 ml-1" />
            </Button>

            <div className="relative">
              <div className="absolute inset-0 flex items-center">
                <span className="w-full border-t border-border" />
              </div>
              <div className="relative flex justify-center text-xs uppercase">
                <span className="bg-background px-2 text-muted-foreground">Or continue with</span>
              </div>
            </div>

            <Button
              type="button"
              variant="outline"
              onClick={handleGoogleSignIn}
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
            Already have an account?{" "}
            <Link
              to="/login"
              state={fromOrgOnboarding ? { fromOrgOnboarding: true, teamName } : undefined}
              className="text-primary hover:underline font-medium"
            >
              Sign in
            </Link>
          </p>
        </div>
      </div>
    </PageShell>
  );
};

export default Signup;
