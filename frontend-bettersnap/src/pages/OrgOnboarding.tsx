import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import PageShell from "@/components/PageShell";
import Navbar from "@/components/Navbar";
import { Input } from "@/components/ui/input";

const OrgOnboarding = () => {
  const [teamName, setTeamName] = useState("");
  const navigate = useNavigate();

  const isValid = teamName.trim().length >= 5;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!isValid) return;
    navigate("/signup", { state: { teamName: teamName.trim(), fromOrgOnboarding: true } });
  };

  return (
    <PageShell>
      <Navbar />
      <div className="min-h-screen flex items-center justify-center px-4 relative">
        <form
          onSubmit={handleSubmit}
          className="glass rounded-2xl shadow-glass p-8 w-full max-w-lg space-y-6"
        >
          <div className="text-center space-y-2">
            <h1 className="text-2xl md:text-3xl font-heading font-bold text-foreground">
              What should we call your team or company?
            </h1>
            <p className="text-muted-foreground text-sm">
              You can change this anytime in your team settings.
            </p>
          </div>

          <Input
            type="text"
            value={teamName}
            onChange={(e) => setTeamName(e.target.value)}
            placeholder="Acme Inc."
            autoFocus
            className="h-12 text-base"
          />

          <button
            type="submit"
            disabled={!isValid}
            className={`w-full h-12 rounded-full font-semibold text-primary-foreground gradient-cta transition-all ${
              isValid ? "hover:opacity-90" : "opacity-50 cursor-not-allowed"
            }`}
          >
            Setup Your Team
          </button>
        </form>

        <Link
          to="/"
          className="absolute bottom-6 left-6 text-sm text-muted-foreground hover:text-foreground transition-colors"
        >
          Get headshots for yourself
        </Link>
      </div>
    </PageShell>
  );
};

export default OrgOnboarding;
