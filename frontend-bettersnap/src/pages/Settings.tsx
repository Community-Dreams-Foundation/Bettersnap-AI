import { useEffect, useState } from "react";
import PageShell from "@/components/PageShell";
import Navbar from "@/components/Navbar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { useAuth } from "@/contexts/AuthContext";
import { authHeaders } from "@/lib/azureApi";
import { useMsal } from "@azure/msal-react";
import { loginRequest } from "@/lib/authConfig";
import { toast } from "sonner";

const Settings = () => {
  const { user } = useAuth();
  const { instance } = useMsal();
  const [profile, setProfile] = useState<{ full_name?: string; [key: string]: any }>({});
  const [savingName, setSavingName] = useState(false);

  const [resettingPassword, setResettingPassword] = useState(false);


  const [emailUpdates, setEmailUpdates] = useState(true);
  const [productAnnouncements, setProductAnnouncements] = useState(false);

  const fetchProfile = async () => {
    try {
      const headers = await authHeaders();
      const res = await fetch(`${import.meta.env.VITE_AZURE_API_URL}/profiles/me`, { headers });
      if (!res.ok) throw new Error("Failed to fetch profile");
      const data = await res.json();
      setProfile(data);
    } catch (err) {
      console.error("Profile fetch failed:", err);
    }
  };

  useEffect(() => {
    if (!user) return;
    fetchProfile();
  }, [user]);

  const handleSave = async () => {
    setSavingName(true);
    try {
      const headers = await authHeaders();
      const res = await fetch(`${import.meta.env.VITE_AZURE_API_URL}/profiles/me`, {
        method: "PATCH",
        headers,
        body: JSON.stringify(profile),
      });
      if (!res.ok) throw new Error("Failed to update profile");
      toast.success("Profile updated");
    } catch (err) {
      toast.error("Update failed");
    } finally {
      setSavingName(false);
    }
  };

  const handleResetPassword = async () => {
    setResettingPassword(true);
    try {
      await instance.loginRedirect({
        ...loginRequest,
        extraQueryParameters: { option: "forgot_password" },
      });
    } catch (error: any) {
      toast.error(error.message || "Couldn't start password reset");
      setResettingPassword(false);
    }
  };


  const handleDeleteAccount = () => {
    toast("Account deletion requested", {
      description: "Please contact support to permanently delete your account.",
    });
  };

  return (
    <PageShell>
      <Navbar />
      <main className="max-w-3xl mx-auto px-4 sm:px-6 lg:px-8 pb-20">
        <header className="mb-10">
          <h1 className="text-4xl font-bold text-foreground">Settings</h1>
          <p className="text-muted-foreground mt-2">Manage your account</p>
        </header>

        <div className="space-y-6">
          {/* Profile Info */}
          <section className="glass rounded-2xl p-6 shadow-glass border border-border bg-card">
            <h2 className="text-xl font-semibold text-foreground mb-4">Profile Info</h2>
            <div className="space-y-4">
              <div>
                <Label className="text-muted-foreground">Email</Label>
                <p className="mt-1 text-foreground">{user?.username ?? "—"}</p>
              </div>
              <div>
                <Label htmlFor="fullName">Name</Label>
                <Input
                  id="fullName"
                  value={profile.full_name ?? ""}
                  onChange={(e) => setProfile({ ...profile, full_name: e.target.value })}
                  placeholder="Your name"
                  className="mt-1"
                />
              </div>
              <Button
                onClick={handleSave}
                disabled={savingName}
                className="gradient-cta text-white"
              >
                {savingName ? "Saving..." : "Save"}
              </Button>
            </div>
          </section>

          {/* Change Password */}
          <section className="glass rounded-2xl p-6 shadow-glass border border-border bg-card">
            <h2 className="text-xl font-semibold text-foreground mb-4">Change Password</h2>
            <p className="text-sm text-muted-foreground mb-4">
              Password changes are handled securely through our identity provider. You'll be redirected to reset your password.
            </p>
            <Button
              onClick={handleResetPassword}
              disabled={resettingPassword}
              className="gradient-cta text-white"
            >
              {resettingPassword ? "Redirecting..." : "Reset password"}
            </Button>
          </section>


          {/* Notification Preferences */}
          <section className="glass rounded-2xl p-6 shadow-glass border border-border bg-card">
            <h2 className="text-xl font-semibold text-foreground mb-4">Notification Preferences</h2>
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-foreground font-medium">Email updates</p>
                  <p className="text-sm text-muted-foreground">Account activity and important changes</p>
                </div>
                <Switch checked={emailUpdates} onCheckedChange={setEmailUpdates} />
              </div>
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-foreground font-medium">Product announcements</p>
                  <p className="text-sm text-muted-foreground">New features and tips from BetterSnap AI</p>
                </div>
                <Switch checked={productAnnouncements} onCheckedChange={setProductAnnouncements} />
              </div>
            </div>
          </section>

          {/* Delete Account */}
          <section className="glass rounded-2xl p-6 shadow-glass border border-border bg-card">
            <h2 className="text-xl font-semibold text-foreground mb-4">Delete Account</h2>
            <Button
              variant="outline"
              onClick={handleDeleteAccount}
              className="border-destructive text-destructive hover:bg-destructive hover:text-destructive-foreground"
            >
              Delete my account
            </Button>
            <p className="text-sm text-muted-foreground mt-3">
              Warning: this permanently removes your account, headshots, and history. This action cannot be undone.
            </p>
          </section>
        </div>
      </main>
    </PageShell>
  );
};

export default Settings;
