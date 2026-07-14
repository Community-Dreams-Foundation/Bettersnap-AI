import { useEffect, useMemo, useState } from "react";
import {
  Check,
  Loader2,
  AlertCircle,
  Briefcase,
  GraduationCap,
  Heart,
  Users,
  Sparkles,
  Wand2,
  Shirt,
  Image as ImageIcon,
  type LucideIcon,
} from "lucide-react";
import { getCatalog, getPlans, type CatalogCategory, type CatalogOption, type Plan } from "@/lib/azureApi";
import { Textarea } from "@/components/ui/textarea";

// Map category ids/types to iconography so the use-case picker feels like a curated
// content grid rather than plain pills. Falls back gracefully for new backend categories.
const CATEGORY_ICONS: Record<string, LucideIcon> = {
  business_suit: Briefcase,
  business: Briefcase,
  corporate: Briefcase,
  linkedin: Briefcase,
  resume: Briefcase,
  academic: GraduationCap,
  student: GraduationCap,
  university: GraduationCap,
  dating: Heart,
  social: Heart,
  personal: Heart,
  personal_branding: Sparkles,
  team: Users,
  organization: Users,
};
const iconFor = (c: CatalogCategory): LucideIcon => {
  if (c.custom) return Wand2;
  return CATEGORY_ICONS[c.id] ?? (c.type === "professional" ? Briefcase : Heart);
};

/**
 * Attire + background picker.
 *
 * Options come from GET /catalog and limits from the user's plan — the SAME modules the
 * backend enforces against (shared/catalog.py, shared/plans.py). Nothing here is
 * hardcoded, so the UI cannot drift from server-side validation. Selections are sent as
 * category-qualified refs, e.g. "business_suit.navy_suit_tie".
 *
 * The onboarding wizard renders this as THREE separate steps via the `section` prop
 * (use-case → attire → background). `activeCat` is lifted to the parent so the choice
 * made on the use-case step is visible to the attire/background steps.
 */

export interface StyleSelection {
  attireRefs: string[];
  backgroundRefs: string[];
  customPrompt: string;
}

export const CUSTOM_SCENE_ID = "custom_scene";

/** Which slice of the picker to render. Omit to render everything (legacy single-step). */
export type StyleSection = "category" | "attire" | "background";

interface Props {
  planKey?: string;
  value: StyleSelection;
  onChange: (next: StyleSelection) => void;
  /** Controlled active category — lifted so the wizard can split it across steps. */
  activeCat: string | null;
  onActiveCatChange: (id: string | null) => void;
  section?: StyleSection;
}

// Fetch catalog + plans ONCE and share across mounts. The wizard mounts this picker on
// three separate steps; without a shared cache each step would refetch /catalog + /plans.
let _cache: { categories: CatalogCategory[]; plans: Plan[] } | null = null;
let _inflight: Promise<{ categories: CatalogCategory[]; plans: Plan[] }> | null = null;
function loadCatalogOnce() {
  if (_cache) return Promise.resolve(_cache);
  if (!_inflight) {
    _inflight = Promise.all([getCatalog(), getPlans()]).then(([categories, plans]) => {
      _cache = { categories, plans };
      return _cache;
    });
  }
  return _inflight;
}

export default function StylePicker({
  planKey,
  value,
  onChange,
  activeCat,
  onActiveCatChange,
  section,
}: Props) {
  const [categories, setCategories] = useState<CatalogCategory[]>(_cache?.categories ?? []);
  const [plans, setPlans] = useState<Plan[]>(_cache?.plans ?? []);
  const [loading, setLoading] = useState(!_cache);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    loadCatalogOnce()
      .then(({ categories: cats, plans: pls }) => {
        if (!alive) return;
        setCategories(cats);
        setPlans(pls);
        if (activeCat == null) {
          onActiveCatChange(cats.find((c) => !c.custom)?.id ?? cats[0]?.id ?? null);
        }
      })
      .catch((e: any) => {
        if (alive) setLoadError(e?.message ?? "Could not load styles");
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const plan = useMemo(
    () => plans.find((p) => p.key === planKey) ?? plans.find((p) => p.key === "trial") ?? plans[0],
    [plans, planKey],
  );

  const category = categories.find((c) => c.id === activeCat);
  const isCustomScene = activeCat === CUSTOM_SCENE_ID;

  // Which "type" (professional | personal) the user has committed to. Plans with
  // category_rule === "single_type" cannot mix the two — the backend rejects it with 403.
  const selectedType = useMemo(() => {
    const refs = [...value.attireRefs, ...value.backgroundRefs];
    for (const ref of refs) {
      const cat = categories.find((c) => c.id === ref.split(".")[0]);
      if (cat) return cat.type;
    }
    return null;
  }, [value.attireRefs, value.backgroundRefs, categories]);

  const singleType = plan?.category_rule === "single_type";
  const typeLocked = (c: CatalogCategory) =>
    singleType && selectedType !== null && c.type !== selectedType && !c.custom;

  const maxAttires = plan?.max_attires ?? 2;
  const maxBackgrounds = plan?.max_backgrounds ?? 2;

  const toggle = (kind: "attire" | "background", ref: string) => {
    const key = kind === "attire" ? "attireRefs" : "backgroundRefs";
    const limit = kind === "attire" ? maxAttires : maxBackgrounds;
    const current = value[key];
    const next = current.includes(ref)
      ? current.filter((r) => r !== ref)
      : current.length >= limit
        ? current
        : [...current, ref];
    onChange({ ...value, [key]: next, customPrompt: "" });
  };

  const setCustom = (text: string) =>
    onChange({ attireRefs: [], backgroundRefs: [], customPrompt: text });

  const combos = value.attireRefs.length * value.backgroundRefs.length;

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12 text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading styles…
      </div>
    );
  }
  if (loadError) {
    return (
      <div className="flex items-center gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-4 text-sm text-destructive">
        <AlertCircle className="h-4 w-4" /> {loadError}
      </div>
    );
  }

  const professional = categories.filter((c) => c.type === "professional" && !c.custom);
  const personal = categories.filter((c) => c.type === "personal" && !c.custom);
  const custom = categories.find((c) => c.custom);

  const showCategory = !section || section === "category";
  const showAttire = !section || section === "attire";
  const showBackground = !section || section === "background";

  const CategoryCards = ({ list }: { list: CatalogCategory[] }) => (
    <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2">
      {list.map((c) => {
        const locked = typeLocked(c);
        const active = activeCat === c.id;
        const Icon = iconFor(c);
        return (
          <button
            key={c.id}
            type="button"
            disabled={locked}
            title={locked ? `Your ${plan?.name} plan can't mix professional and personal styles` : undefined}
            onClick={() => onActiveCatChange(c.id)}
            className={[
              "group relative flex items-center gap-3 rounded-2xl border-2 p-3.5 text-left transition-all duration-200",
              active
                ? "border-primary bg-gradient-to-br from-primary/[0.10] to-primary/[0.02] shadow-[0_8px_24px_-10px_hsl(245_80%_60%/0.4),inset_0_1px_0_0_hsl(245_80%_70%/0.2)]"
                : "border-border bg-card hover:border-primary/40 hover:-translate-y-0.5",
              locked ? "cursor-not-allowed opacity-40 hover:translate-y-0" : "",
            ].join(" ")}
          >
            <div
              className={[
                "flex h-10 w-10 shrink-0 items-center justify-center rounded-xl transition-all",
                active
                  ? "bg-primary text-primary-foreground shadow-[0_4px_14px_-4px_hsl(245_80%_60%/0.6)]"
                  : "bg-secondary/60 text-muted-foreground group-hover:bg-primary/10 group-hover:text-primary",
              ].join(" ")}
            >
              <Icon className="h-4 w-4" />
            </div>
            <p className={["text-sm font-semibold tracking-tight", active ? "text-foreground" : "text-foreground/85"].join(" ")}>
              {c.label}
            </p>
            {active && (
              <span className="ml-auto flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-primary">
                <Check className="h-3 w-3 text-primary-foreground" />
              </span>
            )}
          </button>
        );
      })}
    </div>
  );

  const OptionGrid = ({
    kind,
    options,
    selected,
    limit,
  }: {
    kind: "attire" | "background";
    options: CatalogOption[];
    selected: string[];
    limit: number;
  }) => {
    const KindIcon = kind === "attire" ? Shirt : ImageIcon;
    return (
      <div>
        <div className="mb-3 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <KindIcon className="h-3.5 w-3.5 text-primary" />
            <h4 className="text-[11px] font-semibold uppercase tracking-[0.16em] text-muted-foreground">
              Curated {kind}s
            </h4>
          </div>
          <span className="text-[11px] font-medium text-muted-foreground tabular-nums">
            <span className="text-foreground font-semibold">{selected.length}</span>
            <span className="text-muted-foreground/60"> / {limit}</span>
          </span>
        </div>
        <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3">
          {options.map((o) => {
            const on = selected.includes(o.ref);
            const atLimit = !on && selected.length >= limit;
            return (
              <button
                key={o.ref}
                type="button"
                disabled={atLimit}
                onClick={() => toggle(kind, o.ref)}
                className={[
                  "group relative rounded-2xl border-2 p-3.5 text-left text-sm transition-all duration-200",
                  on
                    ? "border-primary bg-gradient-to-br from-primary/[0.10] to-primary/[0.02] shadow-[0_6px_20px_-8px_hsl(245_80%_60%/0.4),inset_0_1px_0_0_hsl(245_80%_70%/0.2)] font-semibold text-foreground"
                    : "border-border bg-card hover:border-primary/40 text-foreground/85 hover:-translate-y-0.5",
                  atLimit ? "cursor-not-allowed opacity-40 hover:translate-y-0" : "",
                ].join(" ")}
              >
                {on && (
                  <span className="absolute right-2 top-2 flex h-5 w-5 items-center justify-center rounded-full bg-primary shadow">
                    <Check className="h-3 w-3 text-primary-foreground" aria-hidden />
                  </span>
                )}
                <span className="block pr-6 leading-snug">{o.label}</span>
              </button>
            );
          })}
        </div>
      </div>
    );
  };

  return (
    <div className="space-y-6">
      {showCategory && plan && (
        <div className="flex flex-wrap items-center gap-2 rounded-2xl border border-border/60 bg-secondary/30 px-3.5 py-2.5 text-[12px]">
          <span className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-primary">
            {plan.name}
          </span>
          <span className="text-muted-foreground">
            {plan.image_count} images · up to {maxAttires} × {maxBackgrounds}
          </span>
          {singleType && (
            <span className="ml-auto text-[11px] text-muted-foreground/80">
              Can't mix professional &amp; personal
            </span>
          )}
        </div>
      )}

      {showCategory && (
        <div className="space-y-5">
          {professional.length > 0 && (
            <div>
              <p className="mb-2.5 text-[10px] font-semibold uppercase tracking-[0.18em] text-muted-foreground/80">
                Professional
              </p>
              <CategoryCards list={professional} />
            </div>
          )}
          {personal.length > 0 && (
            <div>
              <p className="mb-2.5 text-[10px] font-semibold uppercase tracking-[0.18em] text-muted-foreground/80">
                Personal
              </p>
              <CategoryCards list={personal} />
            </div>
          )}
          {custom && (
            <div>
              <p className="mb-2.5 text-[10px] font-semibold uppercase tracking-[0.18em] text-muted-foreground/80">
                Custom
              </p>
              <CategoryCards list={[custom]} />
            </div>
          )}
        </div>
      )}

      {isCustomScene
        ? showAttire && (
            <div>
              <h4 className="mb-2 text-sm font-medium">Describe your scene</h4>
              <Textarea
                value={value.customPrompt}
                maxLength={400}
                onChange={(e) => setCustom(e.target.value)}
                placeholder="e.g. wearing a linen shirt, sitting at a sunlit café in Rome"
              />
              <p className="mt-1 text-xs text-muted-foreground">
                {value.customPrompt.length}/400 · we add your identity, lighting and quality automatically.
              </p>
            </div>
          )
        : category && (
            <div className="space-y-5">
              {showAttire && (
                <OptionGrid kind="attire" options={category.attires} selected={value.attireRefs} limit={maxAttires} />
              )}
              {showBackground && (
                <OptionGrid
                  kind="background"
                  options={category.backgrounds}
                  selected={value.backgroundRefs}
                  limit={maxBackgrounds}
                />
              )}
            </div>
          )}

      {showBackground && !isCustomScene && (
        <p className="text-xs text-muted-foreground">
          {combos === 0
            ? "Pick at least one attire and one background."
            : `${combos} look${combos === 1 ? "" : "s"} — your ${plan?.image_count ?? ""} images will be spread across them.`}
        </p>
      )}
    </div>
  );
}

/** Is this selection valid to submit? Mirrors the backend's own check. */
export function isSelectionValid(v: StyleSelection): boolean {
  if (v.customPrompt.trim()) return true;
  return v.attireRefs.length > 0 && v.backgroundRefs.length > 0;
}
