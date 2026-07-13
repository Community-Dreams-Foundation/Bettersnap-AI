import { useState, useEffect } from "react";
import { downloadImage } from "@/lib/download";
import { motion, AnimatePresence } from "framer-motion";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { submitJob, pollJobUntilComplete, authHeaders, BASE_URL } from "@/lib/azureApi";
import StylePicker, { isSelectionValid } from "@/components/StylePicker";
import TrainingUpload from "@/components/TrainingUpload";
import TrainingProgress from "@/components/TrainingProgress";
import type { LoraStatus } from "@/lib/azureApi";
import Navbar from "@/components/Navbar";
import PageShell from "@/components/PageShell";
import { Progress } from "@/components/ui/progress";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  FileText,
  IdCard,
  Linkedin,
  Check,
  ArrowLeft,
  Download,
  Loader2,
  AlertCircle,
  Sparkles,
  X,
} from "lucide-react";

const TOTAL_STEPS = 7;

const useCaseOptions = [
  { id: "linkedin", label: "LinkedIn", icon: Linkedin, category: "linkedin" },
  { id: "resume", label: "Resume", icon: FileText, category: "resume" },
  { id: "university", label: "University ID", icon: IdCard, category: "university_id" },
];

const genders = [
  { id: "male", label: "Man" },
  { id: "female", label: "Woman" },
  { id: "other", label: "Other" },
  { id: "prefer_not", label: "Prefer not to say" },
];

const ageRanges = ["18-20", "21-24", "25-29", "30-40", "41-50", "51-65", "65+"];

const hairColors = [
  { id: "black", label: "Black", color: "#111827" },
  { id: "brown", label: "Brown", color: "#92400E" },
  { id: "blonde", label: "Blonde", color: "#FCD34D" },
  { id: "gray", label: "Gray", color: "#9CA3AF" },
  { id: "auburn", label: "Auburn", color: "#7C2D12" },
  { id: "red", label: "Red", color: "#EF4444" },
  { id: "white", label: "White", color: "#F3F4F6" },
  { id: "bald", label: "Bald", color: "#FDE68A" },
];

const StepHeader = ({ step, title, subtitle }: { step: number; title: string; subtitle: string }) => (
  <div className="mb-10">
    <p className="text-xs font-semibold uppercase tracking-widest text-muted-foreground mb-3">
      Step {step} of {TOTAL_STEPS}
    </p>
    <h2 className="text-2xl md:text-3xl font-heading font-bold text-foreground mb-2">{title}</h2>
    <p className="text-muted-foreground">{subtitle}</p>
  </div>
);

const Onboarding = () => {
  const [step, setStep] = useState(1);
  const [selectedCases, setSelectedCases] = useState<string[]>([]);
  const [selectedGender, setSelectedGender] = useState("male");
  const [selectedAge, setSelectedAge] = useState("25-29");
  const [selectedHairColor, setSelectedHairColor] = useState("black");
  // GLOBAL cross-category refs from GET /catalog (new backend contract). The
  // catalog-driven picker populates these; they replace the old purpose/background.
  const [attireRefs, setAttireRefs] = useState<string[]>([]);
  const [backgroundRefs, setBackgroundRefs] = useState<string[]>([]);
  const [customPrompt, setCustomPrompt] = useState("");
  // The user's plan drives the picker's limits (max_attires / max_backgrounds and the
  // single_type rule), so the UI enforces exactly what the server enforces.
  const [planKey, setPlanKey] = useState<string | undefined>(undefined);
  // Drives step 6: none/failed -> upload photos & train; training -> progress screen;
  // ready -> generate. Read from the server so a refresh (or a return visit 30 minutes
  // later) lands the user in the right place.
  const [loraStatus, setLoraStatus] = useState<LoraStatus>("none");
  const [isProcessing, setIsProcessing] = useState(false);
  const [outputImages, setOutputImages] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [statusMsg, setStatusMsg] = useState("Generating your headshot...");
  const [progress, setProgress] = useState(0);
  const [consentGiven, setConsentGiven] = useState(false);
  const [activeTab, setActiveTab] = useState<"headshots" | "compliance">("headshots");

  const navigate = useNavigate();

  // Pull the caller's plan once. /profiles/me returns plan_name (+ lora_status), so the
  // picker's limits come from the same source the backend validates against.
  useEffect(() => {
    (async () => {
      try {
        const headers = await authHeaders();
        const res = await fetch(`${BASE_URL}/profiles/me`, { headers });
        if (res.ok) {
          const p = await res.json();
          setPlanKey(p?.plan_name);
          if (p?.lora_status) setLoraStatus(p.lora_status as LoraStatus);
        }
      } catch {
        /* non-fatal: StylePicker falls back to trial limits */
      }
    })();
  }, []);

  const toggleCase = (id: string) => {
    setSelectedCases((prev) => (prev.includes(id) ? prev.filter((c) => c !== id) : [...prev, id]));
  };

  const handleGenerate = async () => {
    // NO source photo is needed or used. This is txt2img: the likeness comes entirely from
    // the user's trained identity LoRA. The old code bailed out on `if (!uploadedFile)` and
    // ran blur/face preprocessing on that photo — leftovers from the img2img flow. Since we
    // no longer upload one, that guard would have made Generate silently do nothing.
    try {
      setIsProcessing(true);
      setError(null);
      setOutputImages([]);
      setProgress(5);
      setStatusMsg("Submitting job...");
      // GLOBAL cross-category refs from GET /catalog (e.g. "business_suit.navy_suit_tie"),
      // populated by <StylePicker>. No photo is uploaded here: this is txt2img and the
      // likeness comes from the user's trained identity LoRA, not a source image.
      const { job_id, status: submitStatus } = await submitJob({
        gender: selectedGender,
        age_range: selectedAge,
        hair_color: selectedHairColor,
        attire_ids: attireRefs,
        background_ids: backgroundRefs,
        custom_prompt: customPrompt,
      });

      setProgress(50);
      setStatusMsg(
        submitStatus === "waiting_lora"
          ? "We're still building your model — your photos will start automatically."
          : "AI is generating your headshots...",
      );
      const images = await pollJobUntilComplete(job_id, (status) => {
        setStatusMsg(
          status === "waiting_lora"
            ? "Waiting for your model to finish training…"
            : `Status: ${status}...`,
        );
      });
      if (images.length === 0) throw new Error("No images returned.");

      setOutputImages(images);
      setProgress(100);
      setIsProcessing(false);
      setActiveTab("headshots");
      setStep(8);
    } catch (err: any) {
      toast.error("Generation failed", { description: err?.message });
      setError(err?.message || "Something went wrong.");
      setIsProcessing(false);
    }
  };

  const canProceed = () => {
    if (step === 1) return selectedCases.length > 0;
    // Step 6 no longer gates on a single uploaded photo (there isn't one any more). It gates
    // on having a trained model + consent; step 6 renders its own action buttons.
    if (step === 6) return loraStatus === "ready" && consentGiven;
    return true;
  };

  const progressPct = (step / TOTAL_STEPS) * 100;

  const getGenderLabel = () => genders.find((g) => g.id === selectedGender)?.label || selectedGender;
  const getHairLabel = () => hairColors.find((h) => h.id === selectedHairColor)?.label || selectedHairColor;
  const getUseCaseLabel = () => {
    const primaryCase = useCaseOptions.find((uc) => selectedCases.includes(uc.id));
    return primaryCase?.label || "LinkedIn";
  };

  return (
    <PageShell>
      <Navbar />
      <div className="container mx-auto px-4 pb-16 max-w-xl">
        {step <= TOTAL_STEPS && !isProcessing && (
          <div className="mb-8">
            <div className="flex justify-between text-xs text-muted-foreground mb-2">
              <span>Progress</span>
              <span>
                {Math.min(step, TOTAL_STEPS)} of {TOTAL_STEPS}
              </span>
            </div>
            <div className="w-full h-1 bg-secondary rounded-full overflow-hidden">
              <motion.div
                className="h-full gradient-cta rounded-full"
                animate={{ width: `${progressPct}%` }}
                transition={{ duration: 0.3 }}
              />
            </div>
          </div>
        )}

        {/* Error banner. setError() is called from handleGenerate and the training flow, but
            after step 6 was rebuilt nothing rendered `error` any more — so a failed
            generation or a failed training showed the user NOTHING at all. Silent failure is
            worse than an ugly error, so it is surfaced here, above every step. */}
        {error && (
          <div
            role="alert"
            className="mb-6 flex items-start gap-2 rounded-xl border border-destructive/40 bg-destructive/5 p-4"
          >
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
            <div className="flex-1">
              <p className="text-sm text-destructive">{error}</p>
            </div>
            <button
              type="button"
              onClick={() => setError(null)}
              aria-label="Dismiss error"
              className="text-destructive/70 hover:text-destructive"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        )}

        <AnimatePresence mode="wait">
          {/* Step 1 — Use Case */}
          {step === 1 && (
            <motion.div
              key="s1"
              initial={{ opacity: 0, x: 40 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -40 }}
            >
              <StepHeader step={1} title="What do you need a photo for?" subtitle="Select all that apply." />
              <div className="grid grid-cols-3 gap-3">
                {useCaseOptions.map((uc) => {
                  const selected = selectedCases.includes(uc.id);
                  const Icon = uc.icon;
                  return (
                    <button
                      key={uc.id}
                      onClick={() => toggleCase(uc.id)}
                      className={`relative flex flex-col items-center gap-3 p-6 rounded-xl border transition-all ${
                        selected
                          ? "border-primary bg-primary/10 text-foreground"
                          : "border-border glass text-muted-foreground hover:text-foreground hover:border-primary/40"
                      }`}
                    >
                      <Icon className="w-6 h-6" />
                      <span className="text-sm font-medium">{uc.label}</span>
                      {selected && <span className="absolute top-2 right-2 w-2 h-2 rounded-full bg-primary" />}
                    </button>
                  );
                })}
              </div>
              <Button
                onClick={() => setStep(2)}
                disabled={!canProceed()}
                className="w-full mt-8 gradient-cta text-primary-foreground font-semibold btn-glow disabled:opacity-50"
              >
                Continue
              </Button>
            </motion.div>
          )}

          {/* Step 2 — Gender */}
          {step === 2 && (
            <motion.div
              key="s2"
              initial={{ opacity: 0, x: 40 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -40 }}
            >
              <StepHeader
                step={2}
                title="What's your gender?"
                subtitle="Help us generate photos that reflect your appearance."
              />
              <div className="grid grid-cols-2 gap-3">
                {genders.map((g) => (
                  <button
                    key={g.id}
                    onClick={() => setSelectedGender(g.id)}
                    className={`relative flex items-center justify-center p-4 rounded-xl border text-sm font-medium transition-all ${
                      selectedGender === g.id
                        ? "border-primary bg-primary/10 text-foreground"
                        : "border-border glass text-muted-foreground hover:text-foreground hover:border-primary/40"
                    }`}
                  >
                    {g.label}
                    {selectedGender === g.id && (
                      <span className="absolute top-2 right-2 w-2 h-2 rounded-full bg-primary" />
                    )}
                  </button>
                ))}
              </div>
              <div className="flex gap-3 mt-8">
                <Button
                  variant="outline"
                  onClick={() => setStep(1)}
                  className="glass border-border text-foreground gap-1"
                >
                  <ArrowLeft className="w-4 h-4" /> Back
                </Button>
                <Button
                  onClick={() => setStep(3)}
                  className="flex-1 gradient-cta text-primary-foreground font-semibold btn-glow"
                >
                  Continue
                </Button>
              </div>
            </motion.div>
          )}

          {/* Step 3 — Age */}
          {step === 3 && (
            <motion.div
              key="s3"
              initial={{ opacity: 0, x: 40 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -40 }}
            >
              <StepHeader
                step={3}
                title="How old are you?"
                subtitle="We use this to fine-tune generation for your age group."
              />
              <div className="flex flex-wrap gap-2 justify-center">
                {ageRanges.map((a) => (
                  <button
                    key={a}
                    onClick={() => setSelectedAge(a)}
                    className={`px-5 py-2.5 rounded-xl text-sm font-medium transition-all border ${
                      selectedAge === a
                        ? "border-primary bg-primary/10 text-foreground"
                        : "border-border glass text-muted-foreground hover:text-foreground hover:border-primary/40"
                    }`}
                  >
                    {a}
                  </button>
                ))}
              </div>
              <div className="flex gap-3 mt-8">
                <Button
                  variant="outline"
                  onClick={() => setStep(2)}
                  className="glass border-border text-foreground gap-1"
                >
                  <ArrowLeft className="w-4 h-4" /> Back
                </Button>
                <Button
                  onClick={() => setStep(4)}
                  className="flex-1 gradient-cta text-primary-foreground font-semibold btn-glow"
                >
                  Continue
                </Button>
              </div>
            </motion.div>
          )}

          {/* Step 4 — Hair Color */}
          {step === 4 && (
            <motion.div
              key="s4"
              initial={{ opacity: 0, x: 40 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -40 }}
            >
              <StepHeader
                step={4}
                title="What's your hair color?"
                subtitle="Choose the option that closest matches your hair."
              />
              <div className="grid grid-cols-4 gap-4">
                {hairColors.map((h) => (
                  <button
                    key={h.id}
                    onClick={() => setSelectedHairColor(h.id)}
                    className="flex flex-col items-center gap-2"
                  >
                    <div
                      className={`w-14 h-14 rounded-full border-2 transition-all ${
                        selectedHairColor === h.id
                          ? "border-primary scale-110 shadow-lg shadow-primary/20"
                          : "border-border hover:border-primary/50"
                      }`}
                      style={{ backgroundColor: h.color }}
                    />
                    <span className="text-xs text-muted-foreground">{h.label}</span>
                  </button>
                ))}
              </div>
              <div className="flex gap-3 mt-8">
                <Button
                  variant="outline"
                  onClick={() => setStep(3)}
                  className="glass border-border text-foreground gap-1"
                >
                  <ArrowLeft className="w-4 h-4" /> Back
                </Button>
                <Button
                  onClick={() => setStep(5)}
                  className="flex-1 gradient-cta text-primary-foreground font-semibold btn-glow"
                >
                  Continue
                </Button>
              </div>
            </motion.div>
          )}

          {/* Step 5 — Background */}
          {step === 5 && (
            <motion.div
              key="s5"
              initial={{ opacity: 0, x: 40 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -40 }}
            >
              <StepHeader
                step={5}
                title="Choose your styles"
                subtitle="Pick the attire and backgrounds for your headshots."
              />
              {/* The REAL picker, driven by GET /catalog + GET /plans. Replaces the old
                  hardcoded background list, whose ids (clean_white, ...) existed only in the
                  frontend and matched nothing the backend knows — which is why attireRefs /
                  backgroundRefs were always empty and EVERY generation returned 400. */}
              <StylePicker
                planKey={planKey}
                value={{ attireRefs, backgroundRefs, customPrompt }}
                onChange={(next) => {
                  setAttireRefs(next.attireRefs);
                  setBackgroundRefs(next.backgroundRefs);
                  setCustomPrompt(next.customPrompt);
                }}
              />
              <div className="flex gap-3 mt-8">
                <Button
                  variant="outline"
                  onClick={() => setStep(4)}
                  className="glass border-border text-foreground gap-1"
                >
                  <ArrowLeft className="w-4 h-4" /> Back
                </Button>
                <Button
                  onClick={() => setStep(6)}
                  disabled={!isSelectionValid({ attireRefs, backgroundRefs, customPrompt })}
                  className="flex-1 gradient-cta text-primary-foreground font-semibold btn-glow"
                >
                  Continue
                </Button>
              </div>
            </motion.div>
          )}

          {/* Step 6 — Upload Photo */}
          {/* Step 6 — Your model.
              Replaces the old single-photo upload, which uploaded ONE photo (files[0],
              silently dropping the rest) for an img2img flow that no longer exists. The
              likeness now comes from a per-user LoRA trained on 8-12 photos, so this step
              is where that model gets built. */}
          {step === 6 && !isProcessing && (
            <motion.div
              key="s6"
              initial={{ opacity: 0, x: 40 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -40 }}
            >
              {loraStatus === "ready" ? (
                <>
                  <StepHeader
                    step={6}
                    title="Your model is ready"
                    subtitle="We'll generate your headshots from your personal model."
                  />
                  <div className="flex items-center gap-3 rounded-xl border border-border glass p-4">
                    <Check className="w-5 h-5 text-primary" />
                    <span className="text-sm text-foreground">
                      Your personal model is trained and ready to use.
                    </span>
                  </div>

                  <label className="flex items-start gap-3 mt-6 cursor-pointer">
                    <Checkbox
                      checked={consentGiven}
                      onCheckedChange={(c) => setConsentGiven(c === true)}
                      className="mt-0.5"
                    />
                    <span className="text-xs text-muted-foreground leading-relaxed">
                      I understand that my professional headshot will be generated by AI. By
                      proceeding, I consent to my photo being processed for headshot generation
                      only. My image will be retained for up to 30 days and used solely for
                      headshot generation. It will not be shared with third parties.
                    </span>
                  </label>

                  <div className="flex gap-3 mt-8">
                    <Button
                      variant="outline"
                      onClick={() => setStep(5)}
                      className="glass border-border text-foreground gap-1"
                    >
                      <ArrowLeft className="w-4 h-4" /> Back
                    </Button>
                    <Button
                      onClick={handleGenerate}
                      disabled={!consentGiven}
                      className="flex-1 gradient-cta text-primary-foreground font-semibold btn-glow disabled:opacity-50"
                    >
                      <Sparkles className="w-4 h-4 mr-2" /> Generate Headshots
                    </Button>
                  </div>
                </>
              ) : loraStatus === "training" ? (
                <>
                  <StepHeader
                    step={6}
                    title="Building your model"
                    subtitle="This runs on our GPUs and takes about 32 minutes."
                  />
                  <TrainingProgress
                    onReady={() => setLoraStatus("ready")}
                    onFailed={(err) => {
                      setLoraStatus("failed");
                      setError(err || "Training failed. Please try again with different photos.");
                    }}
                  />
                </>
              ) : (
                <>
                  <StepHeader
                    step={6}
                    title="Upload your photos"
                    subtitle="We build a private model of your face from these."
                  />

                  <label className="flex items-start gap-3 mb-6 cursor-pointer">
                    <Checkbox
                      checked={consentGiven}
                      onCheckedChange={(c) => setConsentGiven(c === true)}
                      className="mt-0.5"
                    />
                    <span className="text-xs text-muted-foreground leading-relaxed">
                      I consent to my photos being used to train a private AI model of my face,
                      used solely to generate my own headshots. My photos are retained for up to
                      30 days and are not shared with third parties.
                    </span>
                  </label>

                  {consentGiven ? (
                    <TrainingUpload
                      gender={selectedGender}
                      isRetrain={loraStatus === "failed"}
                      onTrainingStarted={() => setLoraStatus("training")}
                    />
                  ) : (
                    <p className="text-sm text-muted-foreground">
                      Please accept the consent notice above to upload your photos.
                    </p>
                  )}

                  <div className="flex gap-3 mt-8">
                    <Button
                      variant="outline"
                      onClick={() => setStep(5)}
                      className="glass border-border text-foreground gap-1"
                    >
                      <ArrowLeft className="w-4 h-4" /> Back
                    </Button>
                  </div>
                </>
              )}
            </motion.div>
          )}

          {/* Step 7 — Loading */}
          {(step === 7 || (step === 6 && isProcessing)) && isProcessing && (
            <motion.div
              key="loading"
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              className="flex flex-col items-center gap-6 py-20"
            >
              <div className="w-16 h-16 rounded-full gradient-cta flex items-center justify-center glow-primary">
                <Loader2 className="w-8 h-8 text-primary-foreground animate-spin" />
              </div>
              <div className="text-center">
                <p className="text-foreground font-medium text-lg">{statusMsg}</p>
                <p className="text-muted-foreground text-sm mt-1">Please don't close this page</p>
              </div>
              <div className="w-80">
                <Progress value={progress} className="h-1.5 bg-secondary" />
              </div>
            </motion.div>
          )}

          {/* Step 8 — Results */}
          {step === 8 && outputImages.length > 0 && (
            <motion.div
              key="results"
              initial={{ opacity: 0, scale: 0.97 }}
              animate={{ opacity: 1, scale: 1 }}
              className="flex flex-col items-center gap-6 w-full"
            >
              <div className="text-center">
                <h2 className="text-2xl font-heading font-bold text-foreground mb-1">Your Headshots Are Ready</h2>
                <p className="text-muted-foreground text-sm">Download your professional headshots below.</p>
              </div>

              <div className="flex w-full rounded-xl overflow-hidden border border-border glass">
                <button
                  onClick={() => setActiveTab("headshots")}
                  className={`flex-1 py-2.5 text-sm font-medium transition-all ${
                    activeTab === "headshots"
                      ? "bg-primary/10 text-foreground border-b-2 border-primary"
                      : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  Headshots
                </button>
                <button
                  onClick={() => setActiveTab("compliance")}
                  className={`flex-1 py-2.5 text-sm font-medium transition-all ${
                    activeTab === "compliance"
                      ? "bg-primary/10 text-foreground border-b-2 border-primary"
                      : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  Compliance
                </button>
              </div>

              {activeTab === "headshots" && (
                <div className="grid grid-cols-2 gap-4 w-full">
                  {outputImages.map((url, i) => (
                    <div key={i} className="flex flex-col gap-2">
                      <div className="glass rounded-2xl overflow-hidden shadow-glass aspect-square">
                        <img src={url} alt={`Variation ${i + 1}`} className="w-full h-full object-cover" />
                      </div>
                      <Button
                        size="sm"
                        onClick={() => downloadImage(url, `bettersnap-headshot-${i + 1}.jpg`)}
                        className="gradient-cta text-primary-foreground font-medium w-full"
                      >
                        <Download className="w-3 h-3 mr-1" /> Download
                      </Button>
                    </div>
                  ))}
                </div>
              )}

              {activeTab === "compliance" && (
                <div className="w-full glass rounded-2xl border border-border p-5 flex flex-col gap-3">
                  <p className="text-xs font-semibold uppercase tracking-widest text-muted-foreground mb-1">
                    Generation Summary
                  </p>
                  {[
                    { label: "Use Case", value: getUseCaseLabel() },
                    {
                      label: "Styles",
                      value: customPrompt
                        ? "Custom scene"
                        : `${attireRefs.length} attire × ${backgroundRefs.length} background`,
                    },
                    { label: "Gender", value: getGenderLabel() },
                    { label: "Age Range", value: selectedAge },
                    { label: "Hair Color", value: getHairLabel() },
                    { label: "Face Detected", value: "Verified" },
                    { label: "Professional Framing", value: "Verified" },
                  ].map((item) => (
                    <div
                      key={item.label}
                      className="flex items-center justify-between py-2.5 border-b border-border/40 last:border-0"
                    >
                      <div className="flex items-center gap-3">
                        <div className="w-6 h-6 rounded-full bg-green-500/15 flex items-center justify-center flex-shrink-0">
                          <Check className="w-3.5 h-3.5 text-green-500" />
                        </div>
                        <span className="text-sm text-muted-foreground">{item.label}</span>
                      </div>
                      <span className="text-sm font-medium text-foreground">{item.value}</span>
                    </div>
                  ))}
                </div>
              )}

              <div className="flex gap-3 w-full">
                <Button
                  variant="outline"
                  onClick={() => navigate("/history")}
                  className="flex-1 glass border-border text-foreground"
                >
                  View History
                </Button>
                <Button
                  onClick={() => {
                    // Start a NEW generation. The trained model is kept — only the style
                    // selections reset. Retraining is a separate, deliberate action.
                    setStep(1);
                    setOutputImages([]);
                    setSelectedCases([]);
                    setAttireRefs([]);
                    setBackgroundRefs([]);
                    setCustomPrompt("");
                    setActiveTab("headshots");
                  }}
                  className="flex-1 gradient-cta text-primary-foreground"
                >
                  Generate Another
                </Button>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </PageShell>
  );
};

export default Onboarding;
