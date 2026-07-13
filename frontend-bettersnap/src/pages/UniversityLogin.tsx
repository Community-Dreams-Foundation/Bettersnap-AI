import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMsal } from "@azure/msal-react";
import { loginRequest } from "@/lib/authConfig";
import PageShell from "@/components/PageShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Camera, Loader2, ArrowRight, ArrowLeft } from "lucide-react";
import { toast } from "sonner";

const UniversityLogin = () => {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [emailError, setEmailError] = useState("");
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const { instance } = useMsal();



  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!email.trim().toLowerCase().endsWith(".edu")) {
      setEmailError("Please enter a valid university email address");
      return;
    }
    setEmailError("");
    setLoading(true);
    try {
      await instance.loginRedirect({ ...loginRequest, loginHint: email });
    } catch (error: any) {
      toast.error("Login failed", { description: error.message });
      setLoading(false);
    }
  };

  return (
    <PageShell>
      <div className="min-h-screen flex items-center justify-center px-4">
        <div className="glass rounded-2xl shadow-glass p-8 w-full max-w-md space-y-6 relative">
          <Link
            to="/login"
            className="absolute top-4 left-4 p-2 rounded-lg glass border-border text-muted-foreground hover:text-foreground transition-colors"
          >
            <ArrowLeft className="w-5 h-5" />
          </Link>
          <div className="text-center space-y-2 pt-4">
            <div className="w-12 h-12 rounded-xl gradient-cta flex items-center justify-center mx-auto">
              <Camera className="w-6 h-6 text-primary-foreground" />
            </div>
            <h1 className="text-2xl font-heading font-bold text-foreground">University Student Login</h1>
            <p className="text-muted-foreground text-sm">Sign in with your .edu email</p>
          </div>

          <form onSubmit={handleLogin} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="email" className="text-foreground">University Email</Label>
              <Input
                id="email"
                type="email"
                value={email}
                onChange={(e) => {
                  setEmail(e.target.value);
                  if (emailError) setEmailError("");
                }}
                placeholder="you@university.edu"
                required
                className="glass border-border"
                aria-invalid={!!emailError}
              />
              {emailError && (
                <p className="text-sm text-destructive">{emailError}</p>
              )}
            </div>
            <div className="space-y-2">
              <Label htmlFor="password" className="text-foreground">Password</Label>
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
            <Button
              type="submit"
              disabled={loading}
              className="w-full gradient-cta text-primary-foreground btn-glow"
            >
              {loading ? <Loader2 className="w-4 h-4 animate-spin mr-2" /> : null}
              Sign In <ArrowRight className="w-4 h-4 ml-1" />
            </Button>
          </form>

          <p className="text-center text-sm text-muted-foreground">
            Not a student?{" "}
            <Link to="/login" className="text-primary hover:underline font-medium">
              Regular sign in
            </Link>
          </p>
        </div>
      </div>
    </PageShell>
  );
};

export default UniversityLogin;
