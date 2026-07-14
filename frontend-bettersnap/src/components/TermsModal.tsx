import { useEffect, useState } from "react";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { ScrollArea } from "@/components/ui/scroll-area";
import { ArrowLeft, ArrowRight, Check, FileText, Shield, Loader2 } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";
import { toast } from "sonner";
import { getTermsStatus, acceptTerms } from "@/lib/azureApi";

// Terms acceptance is recorded SERVER-SIDE (users.terms_accepted_at) for compliance;
// localStorage is only a fast local cache so the modal doesn't flash on every load.
const TERMS_KEY = "bettersnap_terms_accepted_at";
const perUserKey = (id: string) => `${TERMS_KEY}:${id}`;

const TermsModal = () => {
  const { user } = useAuth();
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState<1 | 2>(1);
  const [checking, setChecking] = useState(true);
  const [saving, setSaving] = useState(false);
  const [ageOk, setAgeOk] = useState(false);
  const [termsOk, setTermsOk] = useState(false);
  const [privacyOk, setPrivacyOk] = useState(false);

  useEffect(() => {
    if (!user) {
      setOpen(false);
      setChecking(false);
      return;
    }
    let cancelled = false;
    (async () => {
      // Server is the source of truth. Fall back to localStorage only if it's
      // unreachable, so a network blip doesn't force a re-accept.
      try {
        const { terms_accepted_at } = await getTermsStatus();
        if (cancelled) return;
        if (terms_accepted_at) {
          localStorage.setItem(perUserKey(user.localAccountId), terms_accepted_at);
          setOpen(false);
        } else {
          setOpen(true); // server: not accepted yet
        }
      } catch {
        const cached =
          localStorage.getItem(perUserKey(user.localAccountId)) ||
          localStorage.getItem(TERMS_KEY);
        if (!cancelled) setOpen(!cached);
      } finally {
        if (!cancelled) setChecking(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [user]);

  const allChecked = ageOk && termsOk && privacyOk;

  const handleAccept = async () => {
    if (!user || !allChecked) return;
    setSaving(true);
    const ts = new Date().toISOString();
    try {
      // Record acceptance SERVER-SIDE first (compliance). Only cache + close on success,
      // so a failed write never makes us think they accepted when the server didn't.
      await acceptTerms(ts);
    } catch (e) {
      console.error("accept-terms failed", e);
      toast.error("Couldn't save your acceptance — please try again.");
      setSaving(false);
      return;
    }
    localStorage.setItem(perUserKey(user.localAccountId), ts);
    localStorage.setItem(TERMS_KEY, ts);
    setSaving(false);
    toast.success("Welcome to BetterSnap AI");
    setOpen(false);
  };

  if (checking || !user) return null;

  return (
    <Dialog open={open} onOpenChange={() => { /* block close */ }}>
      <DialogContent
        className="max-w-2xl bg-white border border-slate-200 rounded-2xl p-0 overflow-hidden shadow-2xl [&>button]:hidden flex flex-col max-h-[90vh]"
        onInteractOutside={(e) => e.preventDefault()}
        onEscapeKeyDown={(e) => e.preventDefault()}
      >
        {/* Header */}
        <div className="p-6 pb-4 border-b border-slate-200 bg-white">
          <DialogHeader>
            <div className="flex items-center gap-3 mb-2">
              <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-purple-600 to-indigo-600 flex items-center justify-center shadow-md">
                {step === 1 ? <FileText className="w-5 h-5 text-white" /> : <Shield className="w-5 h-5 text-white" />}
              </div>
              <div className="flex-1 text-left">
                <DialogTitle className="text-xl font-bold text-slate-900">
                  {step === 1 ? "Terms of Service" : "Privacy Policy"}
                </DialogTitle>
                <DialogDescription className="text-sm text-slate-500">
                  Please review and accept our policies before continuing.
                </DialogDescription>
              </div>
            </div>
            <div className="flex items-center gap-2 pt-2">
              <span className="text-xs font-medium text-slate-500">Step {step} of 2</span>
              <div className="flex-1 h-1.5 bg-slate-100 rounded-full overflow-hidden">
                <div
                  className="h-full bg-gradient-to-r from-purple-600 to-indigo-600 transition-all"
                  style={{ width: step === 1 ? "50%" : "100%" }}
                />
              </div>
            </div>
          </DialogHeader>
        </div>

        {/* Scrollable content */}
        <ScrollArea className="flex-1 px-6 py-5 bg-white">
          {step === 1 ? (
            <div className="space-y-4 text-[15px] text-slate-600 leading-relaxed">
              <div>
                <h3 className="text-slate-900 font-semibold mb-1">1. Use of BetterSnap AI</h3>
                <p>BetterSnap AI provides AI-powered photo generation tools designed to help you create professional headshots and identity photos. By using the service you agree to use it lawfully and respectfully.</p>
              </div>
              <div>
                <h3 className="text-slate-900 font-semibold mb-1">2. Image Ownership</h3>
                <p>You may only upload photos you own or have explicit rights to use. You retain ownership of images you upload. You grant BetterSnap AI a limited license to process them solely to deliver the requested output.</p>
              </div>
              <div>
                <h3 className="text-slate-900 font-semibold mb-1">3. Prohibited Use</h3>
                <p>You may not use BetterSnap AI to create illegal, abusive, misleading, harmful, sexually explicit, or impersonating content, or to violate the rights or privacy of any person.</p>
              </div>
              <div>
                <h3 className="text-slate-900 font-semibold mb-1">4. AI Output</h3>
                <p>AI-generated outputs are produced by machine learning models and may vary in quality, likeness, or accuracy. Results are provided "as is" without guarantees of suitability for a specific purpose.</p>
              </div>
              <div>
                <h3 className="text-slate-900 font-semibold mb-1">5. Service Changes</h3>
                <p>BetterSnap AI may modify, update, or discontinue features at any time to improve the product, address security, or comply with regulations.</p>
              </div>
              <div>
                <h3 className="text-slate-900 font-semibold mb-1">6. Account Suspension</h3>
                <p>Accounts found violating these Terms may be suspended or terminated without prior notice. We reserve the right to remove content that breaches our policies.</p>
              </div>
            </div>
          ) : (
            <div className="space-y-4 text-[15px] text-slate-600 leading-relaxed">
              <div>
                <h3 className="text-slate-900 font-semibold mb-1">1. Photo Processing</h3>
                <p>Photos you upload are transmitted over encrypted connections and processed securely by our AI generation pipeline strictly to produce your requested results.</p>
              </div>
              <div>
                <h3 className="text-slate-900 font-semibold mb-1">2. Personal Data</h3>
                <p>BetterSnap AI handles your personal data responsibly and only collects what is necessary to operate your account and deliver the service.</p>
              </div>
              <div>
                <h3 className="text-slate-900 font-semibold mb-1">3. No Sale of Data</h3>
                <p>We do not sell, rent, or trade your personal data to third parties for marketing purposes.</p>
              </div>
              <div>
                <h3 className="text-slate-900 font-semibold mb-1">4. Storage &amp; Security</h3>
                <p>Your data may be stored to provide service continuity, improve quality, prevent abuse, and ensure security. Reasonable technical and organizational safeguards are applied.</p>
              </div>
              <div>
                <h3 className="text-slate-900 font-semibold mb-1">5. Deletion Requests</h3>
                <p>You may request deletion of your account and associated images at any time, subject to legal retention requirements.</p>
              </div>
              <div>
                <h3 className="text-slate-900 font-semibold mb-1">6. Authentication &amp; Payments</h3>
                <p>BetterSnap AI relies on industry-standard authentication providers and, where applicable, secure payment processors. Sensitive credentials are never stored on our servers in plain form.</p>
              </div>
            </div>
          )}
        </ScrollArea>

        {/* Consent checkboxes (step 2) */}
        {step === 2 && (
          <div className="px-6 py-4 border-t border-slate-200 space-y-3 bg-slate-50">
            <label className="flex items-start gap-3 cursor-pointer">
              <Checkbox checked={ageOk} onCheckedChange={(v) => setAgeOk(v === true)} className="mt-0.5" />
              <span className="text-sm text-slate-700">I confirm I am 18+ or have parental/legal permission.</span>
            </label>
            <label className="flex items-start gap-3 cursor-pointer">
              <Checkbox checked={termsOk} onCheckedChange={(v) => setTermsOk(v === true)} className="mt-0.5" />
              <span className="text-sm text-slate-700">I have read and agree to the Terms &amp; Conditions.</span>
            </label>
            <label className="flex items-start gap-3 cursor-pointer">
              <Checkbox checked={privacyOk} onCheckedChange={(v) => setPrivacyOk(v === true)} className="mt-0.5" />
              <span className="text-sm text-slate-700">I have read and agree to the Privacy Policy.</span>
            </label>
          </div>
        )}

        {/* Fixed bottom actions */}
        <div className="p-4 border-t border-slate-200 flex items-center justify-between gap-3 bg-white">
          <Button
            variant="outline"
            onClick={() => setStep(1)}
            disabled={step === 1 || saving}
            className="bg-white hover:bg-slate-50 text-slate-700 border-slate-200"
          >
            <ArrowLeft className="w-4 h-4 mr-1" /> Back
          </Button>
          {step === 1 ? (
            <Button
              onClick={() => setStep(2)}
              className="bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-700 hover:to-indigo-700 text-white shadow-md"
            >
              Next <ArrowRight className="w-4 h-4 ml-1" />
            </Button>
          ) : (
            <Button
              onClick={handleAccept}
              disabled={!allChecked || saving}
              className="bg-gradient-to-r from-purple-600 to-indigo-600 hover:from-purple-700 hover:to-indigo-700 text-white shadow-md disabled:opacity-50 disabled:cursor-not-allowed"
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
