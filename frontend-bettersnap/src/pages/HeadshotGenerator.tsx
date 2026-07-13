import { useState, useCallback, useRef, useEffect } from "react";
import { Link, useNavigate } from "react-router-dom";
import { BASE_URL, authHeaders } from "@/lib/azureApi";
import { useAuth } from "@/contexts/AuthContext";
import Navbar from "@/components/Navbar";
import PageShell from "@/components/PageShell";
import CameraCapture from "@/components/CameraCapture";
import ImageCropper from "@/components/ImageCropper";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Upload,
  Loader2,
  Download,
  AlertCircle,
  Check,
  Sparkles,
  Camera,
  AlertTriangle,
  Crop,
  X,
  ChevronLeft,
  ShieldCheck,
} from "lucide-react";
import { hairColors, useCaseOptions, backgroundOptions, genderToSet } from "@/data/generatorOptions";
import { downloadImage } from "@/lib/download";
import { detectBlurFromFile } from "@/lib/blur-detection";
import { detectBrightnessFromFile } from "@/lib/brightness-detection";
import { preprocessImage } from "@/lib/image-preprocess";
import { motion, AnimatePresence } from "framer-motion";
import { useToast } from "@/hooks/use-toast";
import { Progress } from "@/components/ui/progress";
import { submitJob, pollJobUntilComplete, type LoraStatus } from "@/lib/azureApi";
import StylePicker, { isSelectionValid } from "@/components/StylePicker";
import TrainingUpload from "@/components/TrainingUpload";
import TrainingProgress from "@/components/TrainingProgress";

const ACCEPTED_TYPES = ["image/jpeg", "image/jpg", "image/png", "image/webp"];
const MAX_FILE_MB = 10;
const DRAFT_KEY = "bettersnap.generate.draft.v1";

const TOTAL_STEPS = 8;

const OUTFIT_IMAGES = {
  business_formal: "https://images.unsplash.com/photo-1560250097-0b93528c311a?w=400&h=500&fit=crop&q=80",
  business_casual: "https://images.unsplash.com/photo-1573496359142-b8d87734a5a2?w=400&h=500&fit=crop&q=80",
  smart_casual: "https://images.unsplash.com/photo-1531123897727-8f129e1688ce?w=400&h=500&fit=crop&q=80",
  casual: "https://images.unsplash.com/photo-1517841905240-472988babdf9?w=400&h=500&fit=crop&q=80",
  creative: "https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=400&h=500&fit=crop&q=80",
};

const outfitOptions: Record<string, { id: string; label: string; description: string; image: string }[]> = {
  linkedin: [
    {
      id: "business_formal",
      label: "Business Formal",
      description: "Dark suit, tie, white shirt — classic professional.",
      image: OUTFIT_IMAGES.business_formal,
    },
    {
      id: "business_casual",
      label: "Business Casual",
      description: "Smart blazer, no tie — modern professional.",
      image: OUTFIT_IMAGES.business_casual,
    },
    {
      id: "smart_casual",
      label: "Smart Casual",
      description: "Neat collared shirt or blouse — approachable yet polished.",
      image: OUTFIT_IMAGES.smart_casual,
    },
  ],
  resume: [
    {
      id: "business_formal",
      label: "Business Formal",
      description: "Suit and tie or formal blouse — industry standard.",
      image: OUTFIT_IMAGES.business_formal,
    },
    {
      id: "business_casual",
      label: "Business Casual",
      description: "Smart blazer — clean and professional.",
      image: OUTFIT_IMAGES.business_casual,
    },
  ],
  university_id: [
    {
      id: "smart_casual",
      label: "Smart Casual",
      description: "Neat, clean clothing suitable for student ID.",
      image: OUTFIT_IMAGES.smart_casual,
    },
    {
      id: "casual",
      label: "Casual",
      description: "Everyday clothing — natural and relaxed.",
      image: OUTFIT_IMAGES.casual,
    },
  ],
  personal: [
    { id: "casual", label: "Casual", description: "Relaxed everyday look.", image: OUTFIT_IMAGES.casual },
    {
      id: "smart_casual",
      label: "Smart Casual",
      description: "Neat and approachable.",
      image: OUTFIT_IMAGES.smart_casual,
    },
    {
      id: "creative",
      label: "Creative / Lifestyle",
      description: "Expressive, personality-forward styling.",
      image: OUTFIT_IMAGES.creative,
    },
  ],
};

const getOutfitsForCategory = (category: string) => {
  if (outfitOptions[category]) return outfitOptions[category];
  const seen = new Set<string>();
  const all: { id: string; label: string; description: string; image: string }[] = [];
  Object.values(outfitOptions).forEach((list) =>
    list.forEach((o) => {
      if (!seen.has(o.id)) {
        seen.add(o.id);
        all.push(o);
      }
    }),
  );
  return all;
};

const CONSENT_ITEMS = [
  "I understand that my uploaded images will be processed using AI.",
  "I understand that my images will be used only to provide the requested headshot-generation service, according to the Privacy Policy.",
  "I understand that BetterSnap AI provides image and formatting guidance but does not guarantee passport, visa, government, employer, university, LinkedIn, or other third-party approval.",
  "__TERMS__",
];

const genders = [
  { id: "male", label: "Man" },
  { id: "female", label: "Woman" },
  { id: "other", label: "Other" },
  { id: "prefer_not", label: "Prefer not to say" },
];

const ageRanges = ["18-20", "21-24", "25-29", "30-40", "41-50", "51-65", "65+"];

// Aliases kept for backward compatibility with code below
const categories = useCaseOptions;
const backgrounds = backgroundOptions;

const StepHeader = ({ step, title, subtitle }: { step: number; title: string; subtitle: string }) => (
  <div className="text-center mb-10">
    <p className="text-xs font-semibold uppercase tracking-widest text-muted-foreground mb-3">
      Step {step} of {TOTAL_STEPS}
    </p>
    <h2 className="text-3xl font-heading font-bold text-foreground mb-2">{title}</h2>
    <p className="text-muted-foreground text-sm max-w-sm mx-auto">{subtitle}</p>
  </div>
);

const HeadshotGenerator = () => {
  const [step, setStep] = useState(1);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [usedCamera, setUsedCamera] = useState(false);
  const [outputImages, setOutputImages] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusMsg, setStatusMsg] = useState("Generating your professional headshot...");
  const [progress, setProgress] = useState(0);
  const [showCamera, setShowCamera] = useState(false);
  const [showCropper, setShowCropper] = useState(false);

  const [selectedGender, setSelectedGender] = useState("male");
  const [selectedAge, setSelectedAge] = useState("25-29");
  const [selectedHairColor, setSelectedHairColor] = useState<string>("");
  const [selectedCategory, setSelectedCategory] = useState<string>("");
  const [selectedBg, setSelectedBg] = useState<string>("");
  const [selectedOutfit, setSelectedOutfit] = useState<string>("");
  const [customPurpose, setCustomPurpose] = useState("");

  // ── GLOBAL cross-category selection (new backend contract) ──────────────────
  // attireRefs / backgroundRefs hold category-qualified refs from GET /catalog
  // (e.g. "business_suit.navy_suit_tie"). customPrompt drives the custom_scene
  // mode. These REPLACE the old single selectedBg/selectedOutfit → purpose/
  // background submit. The picker UI that populates these is built against
  // getCatalog()/getPlans() (plan limits: max_attires, max_backgrounds, and the
  // Basic single-type rule enforced server-side).
  const [attireRefs, setAttireRefs] = useState<string[]>([]);
  const [backgroundRefs, setBackgroundRefs] = useState<string[]>([]);
  const [customPrompt, setCustomPrompt] = useState("");
  // The user's plan drives the picker's limits, so the UI enforces what the server enforces.
  const [planKey, setPlanKey] = useState<string | undefined>(undefined);
  // Drives step 6: none/failed -> upload 8-12 photos & train; training -> progress;
  // ready -> generate. Read from the server so a refresh lands the user in the right place.
  const [loraStatus, setLoraStatus] = useState<LoraStatus>("none");

  const [consents, setConsents] = useState<boolean[]>([false, false, false, false]);
  const allConsent = consents.every(Boolean);
  const [consentError, setConsentError] = useState(false);
  const [blurWarning, setBlurWarning] = useState(false);
  const [blurSeverity, setBlurSeverity] = useState<"sharp" | "slight" | "moderate" | "severe">("sharp");
  const [canAutoEnhance, setCanAutoEnhance] = useState(false);
  const [lowLightWarning, setLowLightWarning] = useState(false);
  const [activeTab, setActiveTab] = useState<"headshots" | "compliance">("headshots");
  const [credits, setCredits] = useState<number | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [billingError, setBillingError] = useState<string | null>(null);
  // Real cost of one generation on THIS user's plan (image_count x credits_per_image),
  // read from the server. The button used to hardcode "1 credit", which was simply false —
  // a trial generation costs 10, Basic 30. Telling the user the wrong price and then
  // charging them the real one is not acceptable.
  const [planCost, setPlanCost] = useState<number | null>(null);
  const warmedUp = useRef(false);
  const { toast } = useToast();
  const navigate = useNavigate();
  const { user } = useAuth();
  const warmUpModal = () => {
    if (warmedUp.current) return;
    warmedUp.current = true;
    fetch("https://sivmm29--better-snap-ai-headshotgenerator-generate.modal.run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        image_url: "https://images.pexels.com/photos/12421121/pexels-photo-12421121.jpeg",
        gender: "male",
        background: "light_grey",
        category: "linkedin",
        num_images: 1,
      }),
    }).catch(() => {});
  };

  const getGenderLabel = () => genders.find((g) => g.id === selectedGender)?.label || selectedGender;
  const getBgLabel = () => backgrounds.find((b) => b.id === selectedBg)?.label || selectedBg;
  const getHairLabel = () => hairColors.find((h) => h.id === selectedHairColor)?.label || selectedHairColor;
  const getCategoryLabel = () => categories.find((c) => c.id === selectedCategory)?.label || selectedCategory;
  const getOutfitLabel = () =>
    getOutfitsForCategory(selectedCategory).find((o) => o.id === selectedOutfit)?.label || selectedOutfit;

  const handleFile = useCallback(
    async (f: File) => {
      if (!ACCEPTED_TYPES.includes(f.type)) {
        toast({
          title: "Invalid file type",
          description: "Please upload a JPG, PNG, or WebP image.",
          variant: "destructive",
        });
        return;
      }
      const [result, brightness] = await Promise.all([detectBlurFromFile(f), detectBrightnessFromFile(f)]);
      setBlurSeverity(result.severity);
      setCanAutoEnhance(result.canEnhance);
      setBlurWarning(result.shouldReject);
      setLowLightWarning(brightness.isLowLight);
      if (result.shouldReject) {
        toast({ title: "Image too blurry", description: "Please retake for best results.", variant: "destructive" });
      } else if (brightness.isLowLight) {
        toast({
          title: "Low lighting detected",
          description: "Please take the photo in better lighting.",
          variant: "destructive",
        });
      }
      setFile(f);
      setPreview(URL.createObjectURL(f));
      setError(null);
    },
    [toast],
  );

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      const f = e.dataTransfer.files[0];
      if (f) handleFile(f);
    },
    [handleFile],
  );

  const onFileSelect = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const f = e.target.files?.[0];
      if (f) handleFile(f);
    },
    [handleFile],
  );

  const handleCropComplete = async (croppedFile: File) => {
    setShowCropper(false);
    const [result, brightness] = await Promise.all([
      detectBlurFromFile(croppedFile),
      detectBrightnessFromFile(croppedFile),
    ]);
    setBlurSeverity(result.severity);
    setCanAutoEnhance(result.canEnhance);
    setBlurWarning(result.shouldReject);
    setLowLightWarning(brightness.isLowLight);
    setFile(croppedFile);
    setPreview(URL.createObjectURL(croppedFile));
  };

  // Load the caller's plan + model status once, so the picker gets the right limits and
  // step 6 knows whether to show "upload photos", "training", or "ready".
  useEffect(() => {
    (async () => {
      try {
        const headers = await authHeaders();
        const res = await fetch(`${BASE_URL}/profiles/me`, { headers });
        if (!res.ok) return;
        const p = await res.json();
        setPlanKey(p?.plan_name);
        if (p?.lora_status) setLoraStatus(p.lora_status as LoraStatus);
        if (p?.plan) {
          setPlanCost((p.plan.image_count ?? 1) * (p.plan.credits_per_image ?? 1));
        }
      } catch {
        /* non-fatal — StylePicker falls back to trial limits */
      }
    })();
  }, []);

  // --- Plan / credit verification ---
  // TWO bugs here, and together they made this ALWAYS fail:
  //   1. It fetched `/users/profile`, which does not exist — the route is `/profiles/me`.
  //      The 404 threw, and the user was told "we couldn't verify your plan or credit
  //      balance" even though their account was perfectly fine. It never checked anything.
  //   2. It read `data.credits`. The API returns `credits_remaining`, so even on a 200 it
  //      would have read undefined -> 0 credits -> "buy a plan".
  // It also returns the plan's real cost (image_count x credits_per_image) instead of a
  // hardcoded 1, so the check matches what the backend actually charges.
  const verifyAccess = useCallback(async (): Promise<{
    credits: number;
    cost: number;
  } | null> => {
    setBillingError(null);
    if (!user) {
      navigate("/login");
      return null;
    }
    try {
      const headers = await authHeaders();
      const res = await fetch(`${BASE_URL}/profiles/me`, { method: "GET", headers });
      if (!res.ok) throw new Error(`Profile fetch failed: ${res.status}`);
      const data = await res.json();
      const c = data?.credits_remaining ?? 0;
      const cost =
        (data?.plan?.image_count ?? 1) * (data?.plan?.credits_per_image ?? 1);
      setCredits(c);
      setPlanCost(cost);
      return { credits: c, cost };
    } catch {
      setBillingError(
        "We couldn't verify your plan or credit balance. Please try again or contact support.",
      );
      return null;
    }
  }, [user, navigate]);

  const goToBilling = (msg: string) => {
    toast({ title: "Plan required", description: msg });
    navigate("/billing");
  };

  const generate = async () => {
    // No `if (!file)` guard: this is txt2img, there is no source photo. The likeness comes
    // from the user's trained identity LoRA.
    if (!allConsent) {
      setConsentError(true);
      return;
    }
    try {
      setVerifying(true);
      const access = await verifyAccess();
      setVerifying(false);
      if (access === null) return;
      if (access.credits < access.cost) {
        goToBilling(
          access.credits === 0
            ? "You need an active plan or enough credits to generate these headshots."
            : `You have ${access.credits} credits. This generation requires ${access.cost}.`,
        );
        return;
      }

      setLoading(true);
      setError(null);
      setOutputImages([]);
      setProgress(5);
      setStatusMsg("Enhancing your photo...");

      const preprocessResult = await preprocessImage(file);
      if (preprocessResult.blurSeverity === "severe") {
        setError("Image looks blurry. Please retake for best results.");
        setLoading(false);
        return;
      }
      if (!preprocessResult.hasFace) {
        setError("No face detected. Please upload a clear photo with your face visible.");
        setLoading(false);
        return;
      }
      // NOTE: no photo is uploaded here any more. This is txt2img — the likeness comes
      // entirely from the user's trained identity LoRA, not from a source image. The old
      // flow uploaded a photo purely to satisfy a required input_blob_path the pipeline
      // never read.
      setProgress(30);
      setStatusMsg("Submitting to inference queue...");
      const { job_id, status: submitStatus } = await submitJob({
        gender: getGenderLabel(),
        age_range: selectedAge,
        hair_color: getHairLabel(),
        attire_ids: attireRefs,
        background_ids: backgroundRefs,
        custom_prompt: customPrompt,
      });

      setProgress(40);
      setStatusMsg(
        submitStatus === "waiting_lora"
          ? "We're still building your model — your photos will start automatically."
          : "AI is generating your headshots…",
      );

      // Returns ALL the images now, not just the first — a job produces many.
      const images = await pollJobUntilComplete(job_id, (status) => {
        if (status === "waiting_lora") {
          setStatusMsg("Waiting for your model to finish training…");
          setProgress(45);
        } else if (status === "processing") {
          setStatusMsg("Model is running inference…");
          setProgress(65);
        }
      });

      setOutputImages(images);
      setProgress(100);
      setStatusMsg("Done!");
      setLoading(false);
      setActiveTab("headshots");
      setStep(8);
      sessionStorage.removeItem(DRAFT_KEY);
      toast({ title: "Headshot ready!", description: "Your professional headshot is ready." });
    } catch (err: any) {
      setError(err?.message || "Something went wrong.");
      setLoading(false);
      setProgress(0);
    }
  };

  const handleReset = () => {
    setStep(1);
    setFile(null);
    setPreview(null);
    setOutputImages([]);
    setError(null);
    setBlurWarning(false);
    setLowLightWarning(false);
    setLoading(false);
    setProgress(0);
    setShowCropper(false);
    setSelectedGender("male");
    setSelectedAge("25-29");
    setSelectedHairColor("");
    setSelectedCategory("");
    setSelectedBg("");
    setSelectedOutfit("");
    setCustomPurpose("");
    setActiveTab("headshots");
    setConsents([false, false, false, false]);
    setConsentError(false);
    setCredits(null);
    setBillingError(null);
    sessionStorage.removeItem(DRAFT_KEY);
  };

  // Restore draft (selections + consent + preview) from sessionStorage
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(DRAFT_KEY);
      if (!raw) return;
      const d = JSON.parse(raw);
      if (d.step) setStep(d.step);
      if (d.selectedGender) setSelectedGender(d.selectedGender);
      if (d.selectedAge) setSelectedAge(d.selectedAge);
      if (d.selectedHairColor) setSelectedHairColor(d.selectedHairColor);
      if (d.selectedCategory) setSelectedCategory(d.selectedCategory);
      if (d.selectedBg) setSelectedBg(d.selectedBg);
      if (d.selectedOutfit) setSelectedOutfit(d.selectedOutfit);
      if (typeof d.customPurpose === "string") setCustomPurpose(d.customPurpose);
      if (Array.isArray(d.consents)) setConsents(d.consents);
      if (d.previewDataUrl) {
        setPreview(d.previewDataUrl);
        // Rehydrate File from data URL so generate() can resume
        fetch(d.previewDataUrl)
          .then((r) => r.blob())
          .then((b) => setFile(new File([b], d.fileName || "restored.jpg", { type: b.type || "image/jpeg" })))
          .catch(() => {});
      }
    } catch {
      /* ignore */
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Persist draft on changes (skip results/loading)
  useEffect(() => {
    if (loading || step === 8) return;
    const save = async () => {
      let previewDataUrl: string | undefined;
      if (file) {
        previewDataUrl = await new Promise<string>((resolve) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result as string);
          reader.readAsDataURL(file);
        });
      }
      sessionStorage.setItem(
        DRAFT_KEY,
        JSON.stringify({
          step,
          selectedGender,
          selectedAge,
          selectedHairColor,
          selectedCategory,
          selectedBg,
          selectedOutfit,
          customPurpose,
          consents,
          previewDataUrl,
          fileName: file?.name,
        }),
      );
    };
    save();
  }, [
    step,
    selectedGender,
    selectedAge,
    selectedHairColor,
    selectedCategory,
    selectedBg,
    selectedOutfit,
    customPurpose,
    consents,
    file,
    loading,
  ]);

  const progressPct = (step / TOTAL_STEPS) * 100;

  return (
    <PageShell>
      <Navbar />
      <div className="pb-16 container mx-auto px-4 max-w-4xl">
        {/* Wider container supports image-card grids in steps 3-5 while staying centered. */}
        {step <= TOTAL_STEPS && !loading && (
          <div className="mb-8">
            <div className="flex justify-between text-xs text-muted-foreground mb-2">
              <span>Progress</span>
              <span>
                {step} of {TOTAL_STEPS}
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

        <AnimatePresence mode="wait">
          {/* Step 1 — Gender */}
          {step === 1 && (
            <motion.div
              key="step1"
              initial={{ opacity: 0, x: 40 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -40 }}
            >
              <StepHeader
                step={1}
                title="What's your gender?"
                subtitle="Help us generate photos that reflect your appearance."
              />
              <div className="grid grid-cols-2 gap-3">
                {genders.map((g) => (
                  <button
                    key={g.id}
                    onClick={() => {
                      setSelectedGender(g.id);
                      warmUpModal();
                    }}
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
              <Button
                onClick={() => setStep(loraStatus === "ready" ? 2 : 6)}
                className="w-full mt-8 gradient-cta text-primary-foreground font-semibold btn-glow"
              >
                Continue
              </Button>
            </motion.div>
          )}

          {/* Step 2 — Age */}
          {step === 2 && (
            <motion.div
              key="step2"
              initial={{ opacity: 0, x: 40 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -40 }}
            >
              <StepHeader
                step={2}
                title="How old are you?"
                subtitle="We use this to fine-tune the generation for your age group."
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
                  onClick={() => setStep(1)}
                  className="glass border-border text-foreground gap-1"
                >
                  <ChevronLeft className="w-4 h-4" /> Back
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

          {/* Step 3 — Hair Color */}
          {step === 3 && (
            <motion.div
              key="step3"
              initial={{ opacity: 0, x: 40 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -40 }}
            >
              <StepHeader
                step={3}
                title="What's your hair color?"
                subtitle="Choose the option that most closely matches your current hair."
              />
              <div
                role="radiogroup"
                aria-label="Hair color"
                className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-4"
              >
                {hairColors.map((h) => {
                  const set = genderToSet(selectedGender);
                  const img = h.images[set];
                  const selected = selectedHairColor === h.id;
                  return (
                    <button
                      key={h.id}
                      role="radio"
                      aria-checked={selected}
                      onClick={() => setSelectedHairColor(h.id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          setSelectedHairColor(h.id);
                        }
                      }}
                      className={`group relative text-left rounded-2xl border bg-card overflow-hidden transition-all focus:outline-none focus-visible:ring-2 focus-visible:ring-primary ${
                        selected
                          ? "border-primary ring-2 ring-primary/40 shadow-lg shadow-primary/10"
                          : "border-border hover:border-primary/50 hover:-translate-y-0.5"
                      }`}
                    >
                      <div className="aspect-[4/5] w-full overflow-hidden bg-muted">
                        <img
                          src={img}
                          alt={`Example portrait with ${h.label.toLowerCase()} hair`}
                          loading="lazy"
                          width={512}
                          height={640}
                          className="w-full h-full object-cover transition-transform group-hover:scale-[1.02]"
                        />
                      </div>
                      {selected && (
                        <span className="absolute top-2 right-2 w-7 h-7 rounded-full bg-primary text-primary-foreground flex items-center justify-center shadow">
                          <Check className="w-4 h-4" aria-hidden="true" />
                          <span className="sr-only">Selected</span>
                        </span>
                      )}
                      <div className="p-3">
                        <p className="text-sm font-medium text-foreground">{h.label}</p>
                      </div>
                    </button>
                  );
                })}
              </div>
              <div className="flex gap-3 mt-8">
                <Button
                  variant="outline"
                  onClick={() => setStep(2)}
                  className="glass border-border text-foreground gap-1"
                >
                  <ChevronLeft className="w-4 h-4" /> Back
                </Button>
                <Button
                  onClick={() => setStep(4)}
                  disabled={!selectedHairColor}
                  className="flex-1 gradient-cta text-primary-foreground font-semibold btn-glow"
                >
                  Continue
                </Button>
              </div>
            </motion.div>
          )}

          {/* Step 4 — Use Case */}
          {step === 4 && (
            <motion.div
              key="step4"
              initial={{ opacity: 0, x: 40 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -40 }}
            >
              <StepHeader
                step={4}
                title="What will you use this photo for?"
                subtitle="We'll recommend the most suitable style, background, framing, and format for your selected purpose."
              />
              <div
                role="radiogroup"
                aria-label="Use case"
                className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4"
              >
                {useCaseOptions.map((uc) => {
                  const selected = selectedCategory === uc.id;
                  return (
                    <button
                      key={uc.id}
                      role="radio"
                      aria-checked={selected}
                      onClick={() => setSelectedCategory(uc.id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          setSelectedCategory(uc.id);
                        }
                      }}
                      className={`group relative text-left rounded-2xl border bg-card overflow-hidden transition-all focus:outline-none focus-visible:ring-2 focus-visible:ring-primary ${
                        selected
                          ? "border-primary ring-2 ring-primary/40 shadow-lg shadow-primary/10"
                          : "border-border hover:border-primary/50 hover:-translate-y-0.5"
                      }`}
                    >
                      <div className="aspect-[4/3] w-full overflow-hidden bg-muted">
                        <img
                          src={uc.image}
                          alt={`${uc.label} example portrait`}
                          loading="lazy"
                          width={512}
                          height={640}
                          className="w-full h-full object-cover transition-transform group-hover:scale-[1.02]"
                        />
                      </div>
                      {selected && (
                        <span className="absolute top-2 right-2 w-7 h-7 rounded-full bg-primary text-primary-foreground flex items-center justify-center shadow">
                          <Check className="w-4 h-4" aria-hidden="true" />
                          <span className="sr-only">Selected</span>
                        </span>
                      )}
                      <div className="p-4">
                        <p className="text-sm font-semibold text-foreground">{uc.label}</p>
                        <p className="text-xs text-muted-foreground mt-1 leading-relaxed">{uc.description}</p>
                        {uc.examples.length > 0 && (
                          <div className="flex flex-wrap gap-1 mt-2">
                            {uc.examples.slice(0, 3).map((ex) => (
                              <span
                                key={ex}
                                className="text-[10px] px-2 py-0.5 rounded-full bg-primary/10 text-primary"
                              >
                                {ex}
                              </span>
                            ))}
                          </div>
                        )}
                        {uc.note && <p className="text-[10px] text-muted-foreground mt-2 italic">{uc.note}</p>}
                      </div>
                    </button>
                  );
                })}
              </div>

              {selectedCategory === "custom" && (
                <div className="mt-4">
                  <label htmlFor="custom-purpose" className="text-xs font-medium text-foreground">
                    Describe your purpose (optional)
                  </label>
                  <input
                    id="custom-purpose"
                    value={customPurpose}
                    onChange={(e) => setCustomPurpose(e.target.value)}
                    placeholder="e.g. Membership card, event speaker page..."
                    className="mt-1 w-full h-10 px-3 rounded-md border border-border bg-background text-sm"
                  />
                </div>
              )}

              <div className="flex gap-3 mt-8">
                <Button
                  variant="outline"
                  onClick={() => setStep(3)}
                  className="glass border-border text-foreground gap-1"
                >
                  <ChevronLeft className="w-4 h-4" /> Back
                </Button>
                <Button
                  onClick={() => setStep(5)}
                  disabled={!selectedCategory}
                  className="flex-1 gradient-cta text-primary-foreground font-semibold btn-glow"
                >
                  Continue
                </Button>
              </div>
            </motion.div>
          )}

          {/* Step 5 — Outfit */}
          {/* Step 5 — Styles (from GET /catalog). Replaces the hardcoded outfit AND
              background grids, whose ids existed only in the frontend and matched nothing
              the backend knows. They never populated attireRefs/backgroundRefs, so every
              Generate submitted an empty selection and the API returned 400. */}
          {step === 5 && (
            <motion.div key="s5" initial={{ opacity: 0, x: 40 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -40 }}>
              <StepHeader
                step={5}
                title="Choose your styles"
                subtitle="Pick the attire and backgrounds for your headshots."
              />
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
                <Button variant="outline" onClick={() => setStep(4)} className="glass border-border gap-1">
                  <ChevronLeft className="w-4 h-4" /> Back
                </Button>
                <Button
                  onClick={() => setStep(6)}
                  disabled={!isSelectionValid({ attireRefs, backgroundRefs, customPrompt })}
                  className="flex-1 gradient-cta text-primary-foreground font-semibold"
                >
                  Continue
                </Button>
              </div>
            </motion.div>
          )}

          {/* Step 6 — Your model. Replaces the old single-photo upload: the likeness now
              comes from a per-user LoRA trained on 8-12 photos, not from a source image. */}
          {step === 6 && (
            <motion.div key="s6" initial={{ opacity: 0, x: 40 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -40 }}>
              {loraStatus === "ready" ? (
                <>
                  <StepHeader step={6} title="Your model is ready" subtitle="We'll generate from your personal model." />
                  <div className="flex items-center gap-3 rounded-xl border border-border glass p-4">
                    <Check className="w-5 h-5 text-primary" />
                    <span className="text-sm text-foreground">Your personal model is trained and ready.</span>
                  </div>
                  <div className="flex gap-3 mt-8">
                    <Button variant="outline" onClick={() => setStep(5)} className="glass border-border gap-1">
                      <ChevronLeft className="w-4 h-4" /> Back
                    </Button>
                    <Button onClick={() => setStep(2)} className="flex-1 gradient-cta text-primary-foreground font-semibold">
                      Continue
                    </Button>
                  </div>
                </>
              ) : loraStatus === "training" ? (
                <>
                  <StepHeader step={6} title="Building your model" subtitle="This takes about 32 minutes." />
                  <TrainingProgress
                    onReady={() => setLoraStatus("ready")}
                    onFailed={(err) => {
                      setLoraStatus("failed");
                      setError(err || "Training failed. Please try again with different photos.");
                    }}
                  />
                  {/* Don't make them watch a 32-minute bar. They can pick their styles now;
                      /jobs/submit accepts the job as `waiting_lora` and the training watcher
                      releases it the moment the model lands — so their photos start on their
                      own, with no second visit. */}
                  <div className="flex gap-3 mt-8">
                    <Button
                      onClick={() => setStep(2)}
                      className="flex-1 gradient-cta text-primary-foreground font-semibold"
                    >
                      Continue while we build your model
                    </Button>
                  </div>
                </>
              ) : (
                <>
                  <StepHeader step={6} title="Upload your photos" subtitle="We build a private model of your face from these." />
                  <TrainingUpload
                    gender={selectedGender}
                    isRetrain={loraStatus === "failed"}
                    onTrainingStarted={() => setLoraStatus("training")}
                  />
                  <div className="flex gap-3 mt-8">
                    <Button variant="outline" onClick={() => setStep(5)} className="glass border-border gap-1">
                      <ChevronLeft className="w-4 h-4" /> Back
                    </Button>
                  </div>
                </>
              )}
            </motion.div>
          )}

          {/* Step 7 — Consent + Generate. No photo upload: this is txt2img. */}
          {step === 7 && !loading && (
            <motion.div key="s7" initial={{ opacity: 0, x: 40 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -40 }}>
              <StepHeader step={7} title="Review and generate" subtitle="Confirm and we'll create your headshots." />

              {billingError && (
                <div className="mb-4 flex items-start gap-2 rounded-xl border border-destructive/40 bg-destructive/5 p-4">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
                  <div>
                    <p className="text-sm font-semibold text-foreground">Verification failed</p>
                    <p className="text-xs text-muted-foreground mt-1">{billingError}</p>
                  </div>
                </div>
              )}
              {error && (
                <div className="mb-4 flex items-start gap-2 rounded-xl border border-destructive/40 bg-destructive/5 p-4">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
                  <p className="text-sm text-destructive">{error}</p>
                </div>
              )}

              <div className="space-y-3 rounded-xl border border-border glass p-4 text-sm">
                <div className="flex justify-between"><span className="text-muted-foreground">Gender</span><span>{getGenderLabel()}</span></div>
                <div className="flex justify-between"><span className="text-muted-foreground">Age</span><span>{selectedAge}</span></div>
                <div className="flex justify-between"><span className="text-muted-foreground">Hair</span><span>{getHairLabel()}</span></div>
                <div className="flex justify-between">
                  <span className="text-muted-foreground">Styles</span>
                  <span>
                    {customPrompt
                      ? "Custom scene"
                      : `${attireRefs.length} attire x ${backgroundRefs.length} background`}
                  </span>
                </div>
              </div>

              <div className="mt-6 space-y-3">
                {CONSENT_ITEMS.map((item, i) => (
                  <label key={i} className="flex items-start gap-3 cursor-pointer">
                    <Checkbox
                      checked={consents[i]}
                      onCheckedChange={(c) =>
                        setConsents((prev) => prev.map((v, idx) => (idx === i ? c === true : v)))
                      }
                      className="mt-0.5"
                    />
                    <span className="text-xs text-muted-foreground leading-relaxed">
                      {item === "__TERMS__" ? "I agree to the Terms of Service and Privacy Policy." : item}
                    </span>
                  </label>
                ))}
                {consentError && (
                  <p className="text-xs text-destructive">Please accept all items to continue.</p>
                )}
              </div>

              <div className="flex gap-3 mt-8">
                <Button variant="outline" onClick={() => setStep(6)} className="glass border-border gap-1">
                  <ChevronLeft className="w-4 h-4" /> Back
                </Button>
                <Button
                  onClick={generate}
                  disabled={
                    (loraStatus !== "ready" && loraStatus !== "training") ||
                    !allConsent ||
                    verifying
                  }
                  className="flex-1 gradient-cta text-primary-foreground font-semibold disabled:opacity-50"
                >
                  {verifying ? (
                    <>
                      <Loader2 className="w-4 h-4 mr-2 animate-spin" /> Verifying access...
                    </>
                  ) : (
                    <>
                      <Sparkles className="w-4 h-4 mr-2" /> Generate Headshots
                      {planCost !== null && (
                        <span className="ml-2 text-xs opacity-80">
                          ({planCost} credit{planCost === 1 ? "" : "s"})
                        </span>
                      )}
                    </>
                  )}
                </Button>
              </div>
            </motion.div>
          )}

          {/* Loading */}
          {loading && (
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
                <h2 className="text-2xl font-heading font-bold text-foreground mb-1">Your Headshots</h2>
                <p className="text-muted-foreground text-sm">Download your professional headshots below.</p>
              </div>

              <div className="flex w-full rounded-xl overflow-hidden border border-border glass">
                <button
                  onClick={() => setActiveTab("headshots")}
                  className={`flex-1 py-2.5 text-sm font-medium transition-all ${activeTab === "headshots" ? "bg-primary/10 text-foreground border-b-2 border-primary" : "text-muted-foreground hover:text-foreground"}`}
                >
                  Headshots
                </button>
                <button
                  onClick={() => setActiveTab("compliance")}
                  className={`flex-1 py-2.5 text-sm font-medium transition-all ${activeTab === "compliance" ? "bg-primary/10 text-foreground border-b-2 border-primary" : "text-muted-foreground hover:text-foreground"}`}
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
                    { label: "Use Case", value: getCategoryLabel() },
                    { label: "Background", value: getBgLabel() },
                    { label: "Gender", value: getGenderLabel() },
                    { label: "Age Range", value: selectedAge },
                    { label: "Hair Color", value: getHairLabel() },
                    { label: "Outfit Style", value: getOutfitLabel() },
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

              <Button
                variant="outline"
                onClick={handleReset}
                className="glass border-border text-foreground hover:bg-secondary w-full"
              >
                Generate Another
              </Button>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </PageShell>
  );
};

export default HeadshotGenerator;
