import { useEffect, useState } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { ScrollArea } from "@/components/ui/scroll-area";
import { ArrowLeft, ArrowRight, Check, FileText, Shield, Loader2 } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";
import { toast } from "sonner";

const TermsModal = () => {
  const { user, getAccessToken } = useAuth();
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState<1 | 2>(1);
  const [checking, setChecking] = useState(true);
  const [saving, setSaving] = useState(false);
  const [ageOk, setAgeOk] = useState(false);
  const [termsOk, setTermsOk] = useState(false);
  const [privacyOk, setPrivacyOk] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const check = async () => {
      if (!user) {
        setOpen(false);
        setChecking(false);
        return;
      }

      // Check localStorage cache first
      const cacheKey = `bsai_terms_accepted_${user.localAccountId}`;
      if (localStorage.getItem(cacheKey)) {
        setOpen(false);
        setChecking(false);
        return;
      }

      // Check from Azure backend
      try {
        const token = await getAccessToken();
        if (!token) {
          setChecking(false);
          return;
        }
        const res = await fetch(
          `${import.meta.env.VITE_API_BASE_URL}/api/user/terms-status`,
          {
            headers: { Authorization: `Bearer ${token}` },
          }
        );
        if (cancelled) return;
        if (res.ok) {
          const data = await res.json();
          if (data.terms_accepted_at) {
            localStorage.setItem(cacheKey, data.terms_accepted_at);
            setOpen(false);
          } else {
            setOpen(true);
          }
        } else {
          // If endpoint doesn't exist yet, use localStorage only
          setOpen(true);
        }
      } catch {
        // Backend not reachable yet — use localStorage only for now
        setOpen(true);
      }
      setChecking(false);
    };
    check();
    return () => { cancelled = true; };
  }, [user, getAccessToken]);

  const allChecked = ageOk && termsOk && privacyOk;

  const handleAccept = async () => {
    if (!user || !allChecked) return;
    setSaving(true);
    const ts = new Date().toISOString();

    try {
      const token = await getAccessToken();
      if (token) {
        await fetch(
          `${import.meta.env.VITE_API_BASE_URL}/api/user/accept-terms`,
          {
            method: "POST",
            headers: {
              Authorization: `Bearer ${token}`,
              "Content-Type": "application/json",
            },
            body: JSON.stringify({ accepted_at: ts }),
          }
        );
      }
    } catch {
      // Backend not reachable yet — continue with localStorage
    }

    localStorage.setItem(`bsai_terms_accepted_${user.localAccountId}`, ts);
    setSaving(false);
    toast.success("Welcome to BetterSnap AI");
    setOpen(false);
  };


  if (checking || !user) return null;

  return (
    <Dialog open={open} onOpenChange={() => { /* block close */ }}>
      <DialogContent
        className="max-w-2xl bg-white border border-border rounded-2xl p-0 overflow-hidden shadow-2xl [&>button]:hidden"
        onInteractOutside={(e) => e.preventDefault()}
        onEscapeKeyDown={(e) => e.preventDefault()}
      >
        <div className="p-6 pb-4 border-b border-border bg-white">
          <DialogHeader>
            <div className="flex items-center gap-3 mb-2">
              <div className="w-10 h-10 rounded-xl gradient-cta flex items-center justify-center shadow-md">
                {step === 1 ? <FileText className="w-5 h-5 text-primary-foreground" /> : <Shield className="w-5 h-5 text-primary-foreground" />}
              </div>
              <div className="flex-1">
                <DialogTitle className="text-xl font-heading font-bold text-foreground">
                  {step === 1 ? "Terms of Service" : "Privacy Policy"}
                </DialogTitle>
                <DialogDescription className="text-sm text-muted-foreground">
                  Please review and accept our policies before continuing.
                </DialogDescription>
              </div>
            </div>
            {/* Progress */}
            <div className="flex items-center gap-2 pt-2">
              <span className="text-xs font-medium text-muted-foreground">Step {step} of 2</span>
              <div className="flex-1 h-1.5 bg-secondary rounded-full overflow-hidden">
                <div
                  className="h-full gradient-cta transition-all"
                  style={{ width: step === 1 ? "50%" : "100%" }}
                />
              </div>
            </div>
          </DialogHeader>
        </div>

        <ScrollArea className="max-h-[45vh] px-6 py-5 bg-white">
          {step === 1 ? (
            <div className="space-y-4 text-[15px] text-muted-foreground leading-relaxed">
              <div>
                <h3 className="text-foreground font-semibold mb-1">1. Use of BetterSnap AI</h3>
                <p>BetterSnap AI provides AI-powered photo generation tools designed to help you create professional headshots and identity photos. By using the service you agree to use it lawfully and respectfully.</p>
              </div>
              <div>
                <h3 className="text-foreground font-semibold mb-1">2. Image Ownership</h3>
                <p>You may only upload photos you own or have explicit rights to use. You retain ownership of images you upload. You grant BetterSnap AI a limited license to process them solely to deliver the requested output.</p>
              </div>
              <div>
                <h3 className="text-foreground font-semibold mb-1">3. Prohibited Use</h3>
                <p>You may not use BetterSnap AI to create illegal, abusive, misleading, harmful, sexually explicit, or impersonating content, or to violate the rights or privacy of any person.</p>
              </div>
              <div>
                <h3 className="text-foreground font-semibold mb-1">4. AI Output</h3>
                <p>AI-generated outputs are produced by machine learning models and may vary in quality, likeness, or accuracy. Results are provided "as is" without guarantees of suitability for a specific purpose.</p>
              </div>
              <div>
                <h3 className="text-foreground font-semibold mb-1">5. Service Changes</h3>
                <p>BetterSnap AI may modify, update, or discontinue features at any time to improve the product, address security, or comply with regulations.</p>
              </div>
              <div>
                <h3 className="text-foreground font-semibold mb-1">6. Account Suspension</h3>
                <p>Accounts found violating these Terms may be suspended or terminated without prior notice. We reserve the right to remove content that breaches our policies.</p>
              </div>
            </div>
          ) : (
            <div className="space-y-4 text-[15px] text-muted-foreground leading-relaxed">
              <div>
                <h3 className="text-foreground font-semibold mb-1">1. Photo Processing</h3>
                <p>Photos you upload are transmitted over encrypted connections and processed securely by our AI generation pipeline strictly to produce your requested results.</p>
              </div>
              <div>
                <h3 className="text-foreground font-semibold mb-1">2. Personal Data</h3>
                <p>BetterSnap AI handles your personal data responsibly and only collects what is necessary to operate your account and deliver the service.</p>
              </div>
              <div>
                <h3 className="text-foreground font-semibold mb-1">3. No Sale of Data</h3>
                <p>We do not sell, rent, or trade your personal data to third parties for marketing purposes.</p>
              </div>
              <div>
                <h3 className="text-foreground font-semibold mb-1">4. Storage &amp; Security</h3>
                <p>Your data may be stored to provide service continuity, improve quality, prevent abuse, and ensure security. Reasonable technical and organizational safeguards are applied.</p>
              </div>
              <div>
                <h3 className="text-foreground font-semibold mb-1">5. Deletion Requests</h3>
                <p>You may request deletion of your account and associated images at any time, subject to legal retention requirements.</p>
              </div>
              <div>
                <h3 className="text-foreground font-semibold mb-1">6. Authentication &amp; Payments</h3>
                <p>BetterSnap AI relies on industry-standard authentication providers and, where applicable, secure payment processors. Sensitive credentials are never stored on our servers in plain form.</p>
              </div>
            </div>
          )}
        </ScrollArea>

        {step === 2 && (
          <div className="px-6 py-4 border-t border-border space-y-3 bg-secondary/40">
            <label className="flex items-start gap-3 cursor-pointer">
              <Checkbox checked={ageOk} onCheckedChange={(v) => setAgeOk(v === true)} className="mt-0.5" />
              <span className="text-sm text-foreground">I confirm I am 18+ or have parental/legal permission.</span>
            </label>
            <label className="flex items-start gap-3 cursor-pointer">
              <Checkbox checked={termsOk} onCheckedChange={(v) => setTermsOk(v === true)} className="mt-0.5" />
              <span className="text-sm text-foreground">I have read and agree to the Terms &amp; Conditions.</span>
            </label>
            <label className="flex items-start gap-3 cursor-pointer">
              <Checkbox checked={privacyOk} onCheckedChange={(v) => setPrivacyOk(v === true)} className="mt-0.5" />
              <span className="text-sm text-foreground">I have read and agree to the Privacy Policy.</span>
            </label>
          </div>
        )}

        <div className="p-4 border-t border-border flex items-center justify-between gap-3 bg-white">
          <Button
            variant="outline"
            onClick={() => setStep(1)}
            disabled={step === 1 || saving}
            className="bg-secondary hover:bg-secondary/80 text-foreground border-border"
          >
            <ArrowLeft className="w-4 h-4 mr-1" /> Back
          </Button>
          {step === 1 ? (
            <Button onClick={() => setStep(2)} className="gradient-cta text-primary-foreground btn-glow hover:opacity-95">
              Next <ArrowRight className="w-4 h-4 ml-1" />
            </Button>
          ) : (
            <Button
              onClick={handleAccept}
              disabled={!allChecked || saving}
              className="gradient-cta text-primary-foreground btn-glow hover:opacity-95 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {saving ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <Check className="w-4 h-4 mr-1" />}
              Accept &amp; Continue
            </Button>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
};

export default TermsModal;
