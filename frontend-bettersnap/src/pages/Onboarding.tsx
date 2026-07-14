import { useState, useEffect, useRef } from "react";
import { downloadImage } from "@/lib/download";
import { motion, AnimatePresence } from "framer-motion";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import {
  submitJob,
  pollJobUntilComplete,
  authHeaders,
  BASE_URL,
  ModelNotTrainedError,
  getTrainingStatus,
  getCatalog,
  getPlans,
  type CatalogCategory,
  type Plan,
} from "@/lib/azureApi";
import StylePicker, { isSelectionValid, CUSTOM_SCENE_ID } from "@/components/StylePicker";
import TrainingUpload from "@/components/TrainingUpload";
import TrainingProgress from "@/components/TrainingProgress";
import type { LoraStatus } from "@/lib/azureApi";
import Navbar from "@/components/Navbar";
import PageShell from "@/components/PageShell";
import { Progress } from "@/components/ui/progress";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Check,
  ArrowLeft,
  ArrowRight,
  Download,
  Loader2,
  AlertCircle,
  Sparkles,
  X,
  User,
  UserCircle,
  Users,
  Calendar,
  Scissors,
  Briefcase,
  Shirt,
  Image as ImageIcon,
  Camera,
  Info,
  Wand2,
  ShieldCheck,
} from "lucide-react";

/**
 * BetterSnap AI Studio — onboarding + generation flow.
 *
 * 7 input steps: Gender → Age → Hair → Use case → Attire → Background → Upload/Train.
 * Backend contract (payload shape, API routes, training/generation flow) is preserved
 * verbatim; this file is the studio-style presentation layer around that flow.
 */

type Stage = "form" | "training" | "generating" | "results";
type StepId = 1 | 2 | 3 | 4 | 5 | 6 | 7;

const FORM_STEPS: { id: StepId; label: string; icon: typeof User }[] = [
  { id: 1, label: "Gender", icon: User },
  { id: 2, label: "Age", icon: Calendar },
  { id: 3, label: "Hair", icon: Scissors },
  { id: 4, label: "Use case", icon: Briefcase },
  { id: 5, label: "Attire", icon: Shirt },
  { id: 6, label: "Background", icon: ImageIcon },
  { id: 7, label: "Photos", icon: Camera },
];

// Minimal option data — the `value` fields are the exact strings the backend expects (unchanged).
interface SimpleOption {
  value: string;
  label: string;
}

const GENDERS: (SimpleOption & { icon: typeof User })[] = [
  { value: "male", label: "Man", icon: User },
  { value: "female", label: "Woman", icon: UserCircle },
  { value: "other", label: "Other", icon: Users },
];

const AGE_RANGES: SimpleOption[] = [
  { value: "18-24", label: "18–24" },
  { value: "25-34", label: "25–34" },
  { value: "35-44", label: "35–44" },
  { value: "45-54", label: "45–54" },
  { value: "55-64", label: "55–64" },
  { value: "65+", label: "65+" },
];

const HAIR_COLORS: (SimpleOption & { swatch: string })[] = [
  { value: "black", label: "Black", swatch: "#1a1a1a" },
  { value: "brown", label: "Brown", swatch: "#6b4423" },
  { value: "blonde", label: "Blonde", swatch: "#e6c97a" },
  { value: "red", label: "Red / Auburn", swatch: "#a34a2a" },
  { value: "gray", label: "Gray / White", swatch: "#b8b8bd" },
  { value: "bald", label: "Bald / Shaved", swatch: "#d8c9b8" },
];

/* ─────────────────────────────── Trackers ─────────────────────────────── */

function LeftTimeline({ current, total }: { current: StepId; total: number }) {
  const visible = FORM_STEPS.slice(0, total);
  const idx = visible.findIndex((s) => s.id === current);
  const pct = ((idx + 1) / visible.length) * 100;
  return (
    <>
      <ol className="relative space-y-0">
        <div className="absolute left-[19px] top-4 bottom-4 w-px bg-gradient-to-b from-border via-border to-border/40" aria-hidden />
        <div
          className="absolute left-[19px] top-4 w-px bg-gradient-to-b from-primary to-primary/40 transition-all duration-500"
          style={{ height: `calc(${pct}% - 2rem)` }}
          aria-hidden
        />
        {visible.map((s, i) => {
          const state: "done" | "current" | "future" = i < idx ? "done" : i === idx ? "current" : "future";
          const Icon = s.icon;
          return (
            <li key={s.id} className="relative flex items-center gap-3.5 py-2">
              <div
                className={[
                  "relative z-10 flex h-10 w-10 items-center justify-center rounded-full border-2 transition-all duration-300",
                  state === "current"
                    ? "border-primary bg-primary text-primary-foreground shadow-[0_0_0_5px_hsl(245_80%_60%/0.15),0_6px_20px_-6px_hsl(245_80%_60%/0.5)]"
                    : state === "done"
                      ? "border-primary/50 bg-primary/10 text-primary"
                      : "border-border bg-card/50 text-muted-foreground/70",
                ].join(" ")}
              >
                {state === "done" ? <Check className="h-4 w-4" /> : <Icon className="h-4 w-4" />}
              </div>
              <p
                className={[
                  "text-[13px] font-semibold tracking-tight transition-colors",
                  state === "current"
                    ? "text-foreground"
                    : state === "done"
                      ? "text-foreground/70"
                      : "text-muted-foreground/60",
                ].join(" ")}
              >
                {s.label}
              </p>
            </li>
          );
        })}
      </ol>
    </>
  );
}

function MobileTracker({ current, total }: { current: StepId; total: number }) {
  const visible = FORM_STEPS.slice(0, total);
  const idx = visible.findIndex((s) => s.id === current);
  const pct = ((idx + 1) / visible.length) * 100;
  const step = visible[idx];
  const Icon = step.icon;
  return (
    <div className="lg:hidden mb-5">
      <div className="mb-2.5 flex items-center justify-between">
        <div className="flex items-center gap-2.5">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl gradient-cta text-primary-foreground shadow-[0_4px_16px_-4px_hsl(245_80%_60%/0.5)]">
            <Icon className="h-4 w-4" />
          </div>
          <div>
            <p className="text-[13px] font-semibold text-foreground leading-tight">{step.label}</p>
            <p className="text-[10px] uppercase tracking-wider text-muted-foreground">
              {idx + 1} / {total}
            </p>
          </div>
        </div>
      </div>
      <div className="h-1 w-full overflow-hidden rounded-full bg-secondary/60">
        <motion.div
          className="h-full gradient-cta rounded-full"
          initial={false}
          animate={{ width: `${pct}%` }}
          transition={{ duration: 0.4, ease: "easeOut" }}
        />
      </div>
    </div>
  );
}

/* ───────────────────────────── Building blocks ───────────────────────────── */

const StudioCard = ({ children, className = "" }: { children: React.ReactNode; className?: string }) => (
  <div
    className={`relative bg-gradient-to-br from-card via-card to-primary/[0.02] rounded-3xl border border-border/80 shadow-[0_12px_50px_-16px_hsl(245_80%_60%/0.2)] p-6 md:p-9 overflow-hidden ${className}`}
  >
    <div
      aria-hidden
      className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-primary/40 to-transparent"
    />
    <div
      aria-hidden
      className="pointer-events-none absolute -top-24 -right-24 h-56 w-56 rounded-full bg-primary/[0.08] blur-3xl"
    />
    <div className="relative">{children}</div>
  </div>
);

const StepHeader = ({ eyebrow, title, subtitle }: { eyebrow: string; title: string; subtitle?: string }) => (
  <div className="mb-7">
    <div className="mb-3 flex items-center gap-2">
      <span className="h-px w-6 bg-primary/60" aria-hidden />
      <p className="text-[10px] font-semibold uppercase tracking-[0.22em] text-primary">{eyebrow}</p>
    </div>
    <h2 className="text-[26px] md:text-[30px] font-heading font-bold text-foreground leading-[1.15] tracking-tight">
      {title}
    </h2>
    {subtitle && <p className="mt-2 text-sm text-muted-foreground max-w-xl">{subtitle}</p>}
  </div>
);

/** Premium icon + label card grid — used by Gender. */
function IconChoiceGrid({
  options,
  value,
  onSelect,
}: {
  options: (SimpleOption & { icon: typeof User })[];
  value: string;
  onSelect: (v: string) => void;
}) {
  return (
    <div className="grid gap-3 grid-cols-3">
      {options.map((o) => {
        const active = value === o.value;
        const Icon = o.icon;
        return (
          <button
            key={o.value}
            type="button"
            onClick={() => onSelect(o.value)}
            className={[
              "group relative flex flex-col items-center justify-center gap-3 rounded-2xl border-2 py-7 px-3 transition-all duration-200",
              active
                ? "border-primary bg-gradient-to-br from-primary/[0.10] to-primary/[0.02] shadow-[0_8px_28px_-10px_hsl(245_80%_60%/0.45),inset_0_1px_0_0_hsl(245_80%_70%/0.2)]"
                : "border-border bg-card hover:border-primary/40 hover:bg-primary/[0.03] hover:-translate-y-0.5",
            ].join(" ")}
          >
            <div
              className={[
                "flex h-12 w-12 items-center justify-center rounded-2xl transition-all",
                active
                  ? "bg-primary text-primary-foreground shadow-[0_4px_16px_-4px_hsl(245_80%_60%/0.6)]"
                  : "bg-secondary/60 text-muted-foreground group-hover:bg-primary/10 group-hover:text-primary",
              ].join(" ")}
            >
              <Icon className="h-5 w-5" />
            </div>
            <p
              className={[
                "text-sm font-semibold tracking-tight",
                active ? "text-foreground" : "text-foreground/80",
              ].join(" ")}
            >
              {o.label}
            </p>
            {active && (
              <span className="absolute right-2.5 top-2.5 flex h-5 w-5 items-center justify-center rounded-full bg-primary shadow">
                <Check className="h-3 w-3 text-primary-foreground" />
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

/** Minimal label-only cards — used by Age. */
function AgeGrid({ value, onSelect }: { value: string; onSelect: (v: string) => void }) {
  return (
    <div className="grid gap-3 grid-cols-2 sm:grid-cols-3">
      {AGE_RANGES.map((o) => {
        const active = value === o.value;
        return (
          <button
            key={o.value}
            type="button"
            onClick={() => onSelect(o.value)}
            className={[
              "relative flex items-center justify-center rounded-2xl border-2 py-5 transition-all duration-200",
              active
                ? "border-primary bg-gradient-to-br from-primary/[0.10] to-primary/[0.02] shadow-[0_8px_28px_-10px_hsl(245_80%_60%/0.45),inset_0_1px_0_0_hsl(245_80%_70%/0.2)]"
                : "border-border bg-card hover:border-primary/40 hover:bg-primary/[0.03] hover:-translate-y-0.5",
            ].join(" ")}
          >
            <p
              className={[
                "text-base font-semibold tracking-tight tabular-nums",
                active ? "text-foreground" : "text-foreground/80",
              ].join(" ")}
            >
              {o.label}
            </p>
            {active && (
              <span className="absolute right-2.5 top-2.5 flex h-5 w-5 items-center justify-center rounded-full bg-primary shadow">
                <Check className="h-3 w-3 text-primary-foreground" />
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

function HairGrid({ value, onSelect }: { value: string; onSelect: (v: string) => void }) {
  return (
    <div className="grid gap-3 grid-cols-2 sm:grid-cols-3">
      {HAIR_COLORS.map((o) => {
        const active = value === o.value;
        return (
          <button
            key={o.value}
            type="button"
            onClick={() => onSelect(o.value)}
            className={[
              "relative flex items-center gap-3 rounded-2xl border-2 p-4 transition-all duration-200",
              active
                ? "border-primary bg-gradient-to-br from-primary/[0.10] to-primary/[0.02] shadow-[0_8px_28px_-10px_hsl(245_80%_60%/0.45),inset_0_1px_0_0_hsl(245_80%_70%/0.2)]"
                : "border-border bg-card hover:border-primary/40 hover:-translate-y-0.5",
            ].join(" ")}
          >
            <span
              className={[
                "h-11 w-11 shrink-0 rounded-full border shadow-inner ring-2 ring-offset-2 ring-offset-card transition-all",
                active ? "ring-primary/40 border-primary/30" : "ring-transparent border-border",
              ].join(" ")}
              style={{ background: o.swatch }}
              aria-hidden
            />
            <p
              className={[
                "text-sm font-semibold tracking-tight",
                active ? "text-foreground" : "text-foreground/80",
              ].join(" ")}
            >
              {o.label}
            </p>
            {active && (
              <span className="absolute right-2.5 top-2.5 flex h-5 w-5 items-center justify-center rounded-full bg-primary shadow">
                <Check className="h-3 w-3 text-primary-foreground" />
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

/* ───────────────────────────── Summary panel ───────────────────────────── */

interface SummaryProps {
  planKey?: string;
  plans: Plan[];
  categories: CatalogCategory[];
  gender: string;
  ageRange: string;
  hairColor: string;
  activeCat: string | null;
  attireRefs: string[];
  backgroundRefs: string[];
  customPrompt: string;
  uploadedCount?: number;
  step: StepId;
}

function labelForRef(ref: string, categories: CatalogCategory[], kind: "attire" | "background") {
  const [catId, optId] = ref.split(".");
  const cat = categories.find((c) => c.id === catId);
  if (!cat) return ref;
  const list = kind === "attire" ? cat.attires : cat.backgrounds;
  return list.find((o) => o.ref === ref || o.ref.endsWith(optId))?.label ?? ref;
}

function SummaryPanel({
  planKey,
  plans,
  categories,
  gender,
  ageRange,
  hairColor,
  activeCat,
  attireRefs,
  backgroundRefs,
  customPrompt,
  step,
}: SummaryProps) {
  const plan = plans.find((p) => p.key === planKey) ?? plans.find((p) => p.key === "trial") ?? plans[0];
  const category = categories.find((c) => c.id === activeCat);
  const genderLabel = GENDERS.find((g) => g.value === gender)?.label;
  const ageLabel = AGE_RANGES.find((a) => a.value === ageRange)?.label;
  const hairLabel = HAIR_COLORS.find((h) => h.value === hairColor)?.label;

  const empty = (targetStep: StepId) =>
    step === targetStep ? (
      <span className="text-primary/80 text-[13px]">Up next</span>
    ) : (
      <span className="text-muted-foreground/50 text-[13px]">—</span>
    );

  const Row = ({ label, children }: { label: string; children: React.ReactNode }) => (
    <div className="flex items-start justify-between gap-3 py-2.5 border-b border-border/40 last:border-0">
      <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground/70">
        {label}
      </span>
      <span className="text-[13px] font-medium text-foreground text-right max-w-[62%] leading-tight">
        {children}
      </span>
    </div>
  );

  return (
    <aside className="relative overflow-hidden rounded-3xl border border-border/80 bg-gradient-to-br from-card via-card to-primary/[0.04] p-5 shadow-[0_12px_40px_-16px_hsl(245_80%_60%/0.2)]">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-primary/40 to-transparent"
      />
      <div className="mb-4 flex items-center gap-2.5">
        <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-primary/10 text-primary ring-1 ring-primary/20">
          <Wand2 className="h-4 w-4" />
        </div>
        <div>
          <p className="text-[13px] font-semibold text-foreground leading-tight tracking-tight">
            Your Headshot Setup
          </p>
          <p className="text-[10px] uppercase tracking-wider text-muted-foreground/70 mt-0.5">
            Live preview
          </p>
        </div>
      </div>

      <div className="space-y-0">
        <Row label="Plan">{plan?.name ?? empty(1)}</Row>
        <Row label="Gender">{genderLabel ?? empty(1)}</Row>
        <Row label="Age">{ageLabel ?? empty(2)}</Row>
        <Row label="Hair">{hairLabel ?? empty(3)}</Row>
        <Row label="Use case">{category?.label ?? empty(4)}</Row>
        <Row label="Attire">
          {customPrompt ? (
            <span className="italic text-muted-foreground">Custom scene</span>
          ) : attireRefs.length > 0 ? (
            <span>{attireRefs.map((r) => labelForRef(r, categories, "attire")).join(", ")}</span>
          ) : (
            empty(5)
          )}
        </Row>
        <Row label="Background">
          {customPrompt ? (
            <span className="italic text-muted-foreground">Included in scene</span>
          ) : backgroundRefs.length > 0 ? (
            <span>{backgroundRefs.map((r) => labelForRef(r, categories, "background")).join(", ")}</span>
          ) : (
            empty(6)
          )}
        </Row>
      </div>

      {plan && (
        <div className="mt-5 rounded-2xl border border-primary/20 bg-gradient-to-br from-primary/[0.06] to-primary/[0.02] p-4">
          <div className="mb-2 flex items-center gap-1.5">
            <Sparkles className="h-3 w-3 text-primary" />
            <p className="text-[10px] font-semibold uppercase tracking-[0.16em] text-primary">
              Included with {plan.name}
            </p>
          </div>
          <ul className="space-y-1.5 text-[12px] text-foreground/85">
            <li className="flex items-center gap-2">
              <span className="h-1 w-1 rounded-full bg-primary/60" />
              {plan.image_count} generated images
            </li>
            <li className="flex items-center gap-2">
              <span className="h-1 w-1 rounded-full bg-primary/60" />
              Up to {plan.max_attires} attire{plan.max_attires === 1 ? "" : "s"} × {plan.max_backgrounds} background
              {plan.max_backgrounds === 1 ? "" : "s"}
            </li>
            <li className="flex items-center gap-2">
              <span className="h-1 w-1 rounded-full bg-primary/60" />
              {plan.category_rule === "single_type"
                ? "Professional or personal"
                : "Professional + personal allowed"}
            </li>
          </ul>
        </div>
      )}
    </aside>
  );
}

function PhotoTips() {
  const tips = [
    "Clear, front-facing photos",
    "Soft, even lighting",
    "A range of angles and expressions",
    "No sunglasses or hats",
    "No group photos or blur",
    "Face fully visible",
  ];
  return (
    <aside className="relative overflow-hidden rounded-3xl border border-border/80 bg-gradient-to-br from-card via-card to-primary/[0.04] p-5 shadow-[0_12px_40px_-16px_hsl(245_80%_60%/0.2)]">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-primary/40 to-transparent"
      />
      <div className="mb-3 flex items-center gap-2.5">
        <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-primary/10 text-primary ring-1 ring-primary/20">
          <Info className="h-4 w-4" />
        </div>
        <div>
          <p className="text-[13px] font-semibold text-foreground leading-tight tracking-tight">
            Photo Quality Guide
          </p>
          <p className="text-[10px] uppercase tracking-wider text-muted-foreground/70 mt-0.5">
            For best likeness
          </p>
        </div>
      </div>
      <ul className="space-y-2">
        {tips.map((t) => (
          <li key={t} className="flex items-start gap-2 text-[12px] text-foreground/85">
            <Check className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
            <span>{t}</span>
          </li>
        ))}
      </ul>
      <div className="mt-4 flex items-start gap-2 rounded-2xl border border-primary/20 bg-primary/[0.05] p-3">
        <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
        <p className="text-[11px] leading-relaxed text-foreground/80">
          Your photos train a private model — used only to generate your headshots.
        </p>
      </div>
    </aside>
  );
}

/* ──────────────────────────────── Onboarding ─────────────────────────────── */

const Onboarding = () => {
  const [stage, setStage] = useState<Stage>("form");
  const [step, setStep] = useState<StepId>(1);

  // Step 1–3: subject attributes
  const [gender, setGender] = useState("male");
  const [ageRange, setAgeRange] = useState("");
  const [hairColor, setHairColor] = useState("");

  // Step 4–6: style (category-qualified refs)
  const [activeCat, setActiveCat] = useState<string | null>(null);
  const [attireRefs, setAttireRefs] = useState<string[]>([]);
  const [backgroundRefs, setBackgroundRefs] = useState<string[]>([]);
  const [customPrompt, setCustomPrompt] = useState("");

  const [planKey, setPlanKey] = useState<string | undefined>(undefined);
  const [loraStatus, setLoraStatus] = useState<LoraStatus>("none");
  const [initializing, setInitializing] = useState(true);

  // Catalog + plans for the summary panel (StylePicker caches these internally).
  const [categories, setCategories] = useState<CatalogCategory[]>([]);
  const [plans, setPlans] = useState<Plan[]>([]);

  const [outputImages, setOutputImages] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [statusMsg, setStatusMsg] = useState("Generating your headshots…");
  const [progress, setProgress] = useState(0);
  const [uploadConsent, setUploadConsent] = useState(false);
  const pendingJobId = useRef<string | null>(null);

  const navigate = useNavigate();

  const isCustomScene = activeCat === CUSTOM_SCENE_ID;
  const totalSteps = loraStatus === "ready" ? 6 : 7;
  const selection = { attireRefs, backgroundRefs, customPrompt };
  const selectionComplete = !!ageRange && !!hairColor && isSelectionValid(selection);

  useEffect(() => {
    (async () => {
      try {
        const headers = await authHeaders();
        const [profileRes, trainStatus, cats, pls] = await Promise.all([
          fetch(`${BASE_URL}/profiles/me`, { headers }).catch(() => null),
          getTrainingStatus().catch(() => null),
          getCatalog().catch(() => [] as CatalogCategory[]),
          getPlans().catch(() => [] as Plan[]),
        ]);
        if (profileRes && profileRes.ok) {
          const p = await profileRes.json();
          setPlanKey(p?.plan_name);
        }
        setCategories(cats);
        setPlans(pls);
        const status: LoraStatus = (trainStatus?.lora_status as LoraStatus) ?? "none";
        setLoraStatus(status);
        if (status === "training") setStage("training");
      } catch {
        /* non-fatal */
      } finally {
        setInitializing(false);
      }
    })();
  }, []);

  const canContinue = (): boolean => {
    switch (step) {
      case 1:
        return !!gender;
      case 2:
        return !!ageRange;
      case 3:
        return !!hairColor;
      case 4:
        return activeCat != null;
      case 5:
        return isCustomScene ? customPrompt.trim().length > 0 : attireRefs.length > 0;
      case 6:
        return isCustomScene ? true : backgroundRefs.length > 0;
      default:
        return false;
    }
  };

  const goBack = () => setStep((s) => (s > 1 ? ((s - 1) as StepId) : s));

  const goNext = () => {
    if (!canContinue()) return;
    if (step === 6) {
      if (loraStatus === "ready") void handleGenerate();
      else setStep(7);
      return;
    }
    setStep((s) => (s < 7 ? ((s + 1) as StepId) : s));
  };

  const handleGenerate = async (preSubmittedJobId?: string) => {
    try {
      setStage("generating");
      setError(null);
      setOutputImages([]);
      setProgress(5);
      let job_id = preSubmittedJobId;
      if (!job_id) {
        setStatusMsg("Submitting job…");
        const res = await submitJob({
          gender,
          age_range: ageRange,
          hair_color: hairColor,
          attire_ids: attireRefs,
          background_ids: backgroundRefs,
          custom_prompt: customPrompt,
        });
        job_id = res.job_id;
      }
      if (!job_id) throw new Error("Could not start generation.");

      setProgress(50);
      setStatusMsg("AI is generating your headshots…");
      const images = await pollJobUntilComplete(job_id, (s) => {
        setStatusMsg(
          s === "waiting_lora"
            ? "Your generation is queued and will start automatically once training is complete."
            : `Status: ${s}…`,
        );
      });
      if (images.length === 0) throw new Error("No images returned.");
      setOutputImages(images);
      setProgress(100);
      setStage("results");
    } catch (err: any) {
      if (err instanceof ModelNotTrainedError) {
        setLoraStatus(err.loraStatus);
        setStage(err.loraStatus === "training" ? "training" : "form");
        if (err.loraStatus !== "training") setStep(7);
        toast.info("Please train your model first", {
          description: "Upload 8–12 photos to build your personal model.",
        });
        return;
      }
      toast.error("Generation failed", { description: err?.message });
      setError(err?.message || "Something went wrong.");
      setStage("form");
      setStep(6);
    }
  };

  if (initializing) {
    return (
      <PageShell>
        <Navbar />
        <div className="container mx-auto px-4 py-24 flex justify-center">
          <Loader2 className="w-6 h-6 animate-spin text-primary" />
        </div>
      </PageShell>
    );
  }

  const showStudioLayout = stage === "form";

  const stepCopy: Record<StepId, { eyebrow: string; title: string; subtitle?: string }> = {
    1: {
      eyebrow: "Identity",
      title: "Choose your headshot direction",
    },
    2: {
      eyebrow: "Age range",
      title: "Select your age range",
    },
    3: {
      eyebrow: "Hair",
      title: "Pick your hair colour",
    },
    4: {
      eyebrow: "Use case",
      title: "Where will you use these headshots?",
      subtitle: "We'll tailor the attire and background library to match your goal.",
    },
    5: {
      eyebrow: "Attire",
      title: isCustomScene ? "Describe your scene" : "Curate your looks",
      subtitle: isCustomScene
        ? "Tell BetterSnap AI what to put you in and where."
        : "Pick the outfits BetterSnap AI will style you in.",
    },
    6: {
      eyebrow: "Background",
      title: isCustomScene ? "Review your scene" : "Set the scene",
      subtitle: isCustomScene
        ? "Your custom scene sets the background — nothing more to pick."
        : "Choose the environments for your final headshots.",
    },
    7: {
      eyebrow: "Train your model",
      title: "Upload 8–12 clear photos",
      subtitle: "These train your private BetterSnap AI model — used only for your headshots.",
    },
  };

  return (
    <PageShell>
      <Navbar />
      <div className="container mx-auto px-4 pt-8 pb-16 max-w-6xl">
        {/* Compact header */}
        <div className="mb-7 text-center lg:text-left">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-primary/20 bg-primary/[0.06] px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.18em] text-primary">
            <Sparkles className="h-3 w-3" /> BetterSnap AI Studio
          </span>
          <h1 className="mt-3 text-[26px] md:text-[34px] font-heading font-bold text-foreground leading-[1.1] tracking-tight">
            Create your professional AI headshots
          </h1>
          <p className="mt-2 text-sm text-muted-foreground max-w-xl lg:mx-0 mx-auto">
            A guided studio experience — personalize your look, curate your style, and train your private AI model.
          </p>
        </div>

        {error && (
          <div
            role="alert"
            className="mb-6 flex items-start gap-2 rounded-2xl border border-destructive/40 bg-destructive/5 p-4"
          >
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
            <p className="flex-1 text-sm text-destructive">{error}</p>
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

        {showStudioLayout ? (
          <div className="grid gap-6 lg:grid-cols-[210px_minmax(0,1fr)_300px]">
            {/* Left rail — desktop timeline */}
            <div className="hidden lg:block">
              <div className="sticky top-24 rounded-3xl border border-border/80 bg-gradient-to-b from-card/80 to-card/40 backdrop-blur-sm p-5 shadow-[0_8px_32px_-16px_hsl(245_80%_60%/0.2)]">
                <div className="mb-4 flex items-center gap-2">
                  <span className="h-px w-4 bg-primary/60" />
                  <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-muted-foreground/80">
                    Your journey
                  </p>
                </div>
                <LeftTimeline current={step} total={totalSteps} />
              </div>
            </div>

            {/* Center — main step */}
            <div className="min-w-0">
              <MobileTracker current={step} total={totalSteps} />
              <AnimatePresence mode="wait">
                <motion.div
                  key={`form-${step}`}
                  initial={{ opacity: 0, y: 12 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -12 }}
                  transition={{ duration: 0.25 }}
                >
                  <StudioCard>
                    <StepHeader {...stepCopy[step]} />

                    {step === 1 && <IconChoiceGrid options={GENDERS} value={gender} onSelect={setGender} />}
                    {step === 2 && <AgeGrid value={ageRange} onSelect={setAgeRange} />}
                    {step === 3 && <HairGrid value={hairColor} onSelect={setHairColor} />}

                    {(step === 4 || step === 5 || step === 6) && (
                      <StylePicker
                        section={step === 4 ? "category" : step === 5 ? "attire" : "background"}
                        planKey={planKey}
                        activeCat={activeCat}
                        onActiveCatChange={setActiveCat}
                        value={selection}
                        onChange={(n) => {
                          setAttireRefs(n.attireRefs);
                          setBackgroundRefs(n.backgroundRefs);
                          setCustomPrompt(n.customPrompt);
                        }}
                      />
                    )}
                    {step === 6 && isCustomScene && (
                      <div className="mt-3 rounded-2xl border border-border bg-secondary/40 p-4 text-sm text-muted-foreground">
                        "{customPrompt}"
                      </div>
                    )}

                    {step === 7 && (
                      <>
                        <label className="mb-6 flex cursor-pointer items-start gap-3 rounded-2xl border border-primary/20 bg-gradient-to-br from-primary/[0.06] to-primary/[0.02] p-4 hover:border-primary/40 transition-colors">
                          <Checkbox
                            checked={uploadConsent}
                            onCheckedChange={(c) => setUploadConsent(c === true)}
                            className="mt-0.5"
                          />
                          <div className="flex-1">
                            <p className="text-[13px] font-semibold text-foreground mb-0.5">
                              I consent to model training on my photos
                            </p>
                            <p className="text-[11px] leading-relaxed text-muted-foreground">
                              Your photos train a private BetterSnap AI model — used only to generate your own headshots.
                            </p>
                          </div>
                        </label>
                        {uploadConsent ? (
                          <TrainingUpload
                            gender={gender}
                            isRetrain={loraStatus === "failed"}
                            onTrainingStarted={async () => {
                              setLoraStatus("training");
                              setStage("training");
                              try {
                                const { job_id } = await submitJob({
                                  gender,
                                  age_range: ageRange,
                                  hair_color: hairColor,
                                  attire_ids: attireRefs,
                                  background_ids: backgroundRefs,
                                  custom_prompt: customPrompt,
                                });
                                pendingJobId.current = job_id;
                              } catch {
                                pendingJobId.current = null;
                              }
                            }}
                          />
                        ) : (
                          <div className="rounded-2xl border border-dashed border-border bg-secondary/30 p-6 text-center">
                            <p className="text-sm text-muted-foreground">
                              Accept the consent notice above to continue.
                            </p>
                          </div>
                        )}
                      </>
                    )}

                    {/* Nav */}
                    <div className="mt-8 flex gap-3">
                      {step > 1 && (
                        <Button
                          variant="outline"
                          onClick={goBack}
                          className="gap-1 border-border text-foreground hover:bg-secondary"
                        >
                          <ArrowLeft className="h-4 w-4" /> Back
                        </Button>
                      )}
                      {step < 7 && (
                        <Button
                          onClick={goNext}
                          disabled={!canContinue()}
                          className="h-12 flex-1 gradient-cta font-semibold text-primary-foreground btn-glow disabled:opacity-50 group"
                        >
                          {step === 6 && loraStatus === "ready" ? (
                            <>
                              <Sparkles className="mr-2 h-4 w-4" /> Generate Headshots
                            </>
                          ) : (
                            <>
                              Continue{" "}
                              <ArrowRight className="ml-1 h-4 w-4 transition-transform group-hover:translate-x-0.5" />
                            </>
                          )}
                        </Button>
                      )}
                    </div>
                  </StudioCard>
                </motion.div>
              </AnimatePresence>
            </div>

            {/* Right — summary / tips */}
            <div className="min-w-0">
              <div className="lg:sticky lg:top-24 space-y-4">
                <SummaryPanel
                  planKey={planKey}
                  plans={plans}
                  categories={categories}
                  gender={gender}
                  ageRange={ageRange}
                  hairColor={hairColor}
                  activeCat={activeCat}
                  attireRefs={attireRefs}
                  backgroundRefs={backgroundRefs}
                  customPrompt={customPrompt}
                  step={step}
                />
                {step === 7 && <PhotoTips />}
              </div>
            </div>
          </div>
        ) : (
          <div className="mx-auto max-w-3xl">
            <AnimatePresence mode="wait">
              {stage === "training" && (
                <motion.div
                  key="training"
                  initial={{ opacity: 0, y: 12 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -12 }}
                >
                  <StudioCard>
                    <StepHeader
                      eyebrow="Training in progress"
                      title="Training your personal AI model"
                      subtitle="This usually takes around 30–35 minutes on our GPUs."
                    />
                    <div className="rounded-2xl border border-primary/20 bg-gradient-to-br from-primary/[0.06] to-transparent p-6">
                      <TrainingProgress
                        onReady={() => {
                          setLoraStatus("ready");
                          if (pendingJobId.current) {
                            void handleGenerate(pendingJobId.current);
                          } else if (selectionComplete) {
                            void handleGenerate();
                          } else {
                            setStage("form");
                            setStep(2);
                            toast.info("Your model is ready", { description: "Pick your style and generate." });
                          }
                        }}
                        onFailed={(err) => {
                          setLoraStatus("failed");
                          setError(err || "Training failed. Please try again with different photos.");
                          setStage("form");
                          setStep(7);
                        }}
                      />
                    </div>
                    <div className="mt-6 flex items-start gap-2.5 rounded-2xl border border-border bg-secondary/60 p-4">
                      <Sparkles className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                      <p className="text-xs leading-relaxed text-foreground/80">
                        You can leave this page and come back. Your progress will continue in the background, and your
                        generation will start automatically once the model is ready.
                      </p>
                    </div>
                  </StudioCard>
                </motion.div>
              )}

              {stage === "generating" && (
                <motion.div key="generating" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
                  <StudioCard className="flex flex-col items-center gap-6 py-16">
                    <div className="relative">
                      <div className="flex h-20 w-20 items-center justify-center rounded-full gradient-cta glow-primary">
                        <Sparkles className="h-8 w-8 text-primary-foreground" />
                      </div>
                      <Loader2 className="absolute inset-0 m-auto h-20 w-20 animate-spin text-primary/40" />
                    </div>
                    <div className="max-w-sm text-center">
                      <p className="text-lg font-semibold text-foreground">{statusMsg}</p>
                      <p className="mt-1 text-sm text-muted-foreground">
                        Please keep this page open — we're generating your headshots.
                      </p>
                    </div>
                    <div className="w-full max-w-sm">
                      <Progress value={progress} className="h-2 bg-secondary" />
                    </div>
                  </StudioCard>
                </motion.div>
              )}

              {stage === "results" && outputImages.length > 0 && (
                <motion.div key="results" initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }}>
                  <StudioCard>
                    <div className="mb-6 text-center">
                      <div className="mb-3 inline-flex h-12 w-12 items-center justify-center rounded-full bg-primary/10">
                        <Check className="h-6 w-6 text-primary" />
                      </div>
                      <h2 className="mb-1 text-2xl font-heading font-bold text-foreground md:text-3xl">
                        Download your finished headshots
                      </h2>
                      <p className="text-sm text-muted-foreground">
                        {outputImages.length} professional headshot{outputImages.length === 1 ? "" : "s"} ready to
                        download.
                      </p>
                    </div>

                    <div className="grid grid-cols-2 gap-4 md:grid-cols-3">
                      {outputImages.map((url, i) => (
                        <div key={i} className="group flex flex-col gap-2">
                          <div className="relative aspect-square overflow-hidden rounded-2xl border border-border bg-secondary shadow-sm">
                            <img
                              src={url}
                              alt={`Headshot ${i + 1}`}
                              className="h-full w-full object-cover transition-transform duration-500 group-hover:scale-[1.03]"
                            />
                          </div>
                          <Button
                            size="sm"
                            onClick={() => downloadImage(url, `bettersnap-headshot-${i + 1}.jpg`)}
                            className="w-full gradient-cta font-medium text-primary-foreground"
                          >
                            <Download className="mr-1.5 h-3.5 w-3.5" /> Download
                          </Button>
                        </div>
                      ))}
                    </div>

                    <div className="mt-8 flex gap-3">
                      <Button
                        variant="outline"
                        onClick={() => navigate("/dashboard")}
                        className="flex-1 border-border text-foreground"
                      >
                        Back to Dashboard
                      </Button>
                      <Button
                        onClick={() => {
                          setOutputImages([]);
                          setStage("form");
                          setStep(4);
                        }}
                        className="flex-1 gradient-cta font-semibold text-primary-foreground"
                      >
                        <Sparkles className="mr-2 h-4 w-4" /> Generate More
                      </Button>
                    </div>
                  </StudioCard>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        )}
      </div>
    </PageShell>
  );
};

export default Onboarding;
