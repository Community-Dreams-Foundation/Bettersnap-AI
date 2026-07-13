import { useEffect, useMemo, useState } from "react";
import { Check, Loader2, AlertCircle } from "lucide-react";
import {
  getCatalog,
  getPlans,
  type CatalogCategory,
  type CatalogOption,
  type Plan,
} from "@/lib/azureApi";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";

/**
 * Attire + background picker.
 *
 * WHY THIS EXISTS: the old flow declared `attireRefs`/`backgroundRefs` state but nothing
 * ever populated them, so EVERY generation submitted an empty selection and the backend
 * rejected it with 400 ("Select at least one attire and one background"). Generation from
 * the UI was broken for every user, including ones with a trained model.
 *
 * Options come from GET /catalog and limits from the user's plan — the SAME modules the
 * backend enforces against (shared/catalog.py, shared/plans.py). Nothing here is
 * hardcoded, so the UI cannot drift from server-side validation. Selections are sent as
 * category-qualified refs, e.g. "business_suit.navy_suit_tie".
 */

export interface StyleSelection {
  attireRefs: string[];
  backgroundRefs: string[];
  customPrompt: string;
}

interface Props {
  planKey?: string;
  value: StyleSelection;
  onChange: (next: StyleSelection) => void;
}

const CUSTOM_SCENE_ID = "custom_scene";

export default function StylePicker({ planKey, value, onChange }: Props) {
  const [categories, setCategories] = useState<CatalogCategory[]>([]);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [activeCat, setActiveCat] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const [cats, pls] = await Promise.all([getCatalog(), getPlans()]);
        setCategories(cats);
        setPlans(pls);
        setActiveCat(cats.find((c) => !c.custom)?.id ?? cats[0]?.id ?? null);
      } catch (e: any) {
        setLoadError(e?.message ?? "Could not load styles");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const plan = useMemo(
    () => plans.find((p) => p.key === planKey) ?? plans.find((p) => p.key === "trial") ?? plans[0],
    [plans, planKey],
  );

  const category = categories.find((c) => c.id === activeCat);
  const isCustomScene = activeCat === CUSTOM_SCENE_ID;

  // Which "type" (professional | personal) the user has already committed to. Plans with
  // category_rule === "single_type" (trial/basic) cannot mix the two — the backend rejects
  // it with 403, so we must not let them build an invalid selection in the first place.
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
        ? current // at the plan's limit — ignore rather than silently drop another pick
        : [...current, ref];
    // Picking a menu style means we are NOT in custom-scene mode.
    onChange({ ...value, [key]: next, customPrompt: "" });
  };

  const setCustom = (text: string) =>
    // custom_scene replaces the menu entirely (the backend skips attire/background when
    // custom_prompt is present), so clear the menu picks to keep client and server agreed.
    onChange({ attireRefs: [], backgroundRefs: [], customPrompt: text });

  // The backend computes images as: image_count spread over (attires x backgrounds).
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

  const CategoryButtons = ({ list }: { list: CatalogCategory[] }) => (
    <div className="flex flex-wrap gap-2">
      {list.map((c) => {
        const locked = typeLocked(c);
        return (
          <Button
            key={c.id}
            type="button"
            size="sm"
            variant={activeCat === c.id ? "default" : "outline"}
            disabled={locked}
            title={
              locked
                ? `Your ${plan?.name} plan can't mix professional and personal styles`
                : undefined
            }
            onClick={() => setActiveCat(c.id)}
          >
            {c.label}
          </Button>
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
  }) => (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <h4 className="text-sm font-medium capitalize">{kind}s</h4>
        <span className="text-xs text-muted-foreground">
          {selected.length}/{limit} selected
        </span>
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
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
                "relative rounded-lg border p-3 text-left text-sm transition",
                on
                  ? "border-primary bg-primary/10 font-medium"
                  : "border-border hover:border-primary/50",
                atLimit ? "cursor-not-allowed opacity-40" : "",
              ].join(" ")}
            >
              {on && (
                <Check className="absolute right-2 top-2 h-4 w-4 text-primary" aria-hidden />
              )}
              {o.label}
            </button>
          );
        })}
      </div>
    </div>
  );

  return (
    <div className="space-y-6">
      {plan && (
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Badge variant="secondary">{plan.name}</Badge>
          <span className="text-muted-foreground">
            {plan.image_count} images · up to {maxAttires} attires × {maxBackgrounds} backgrounds
          </span>
          {singleType && (
            <span className="text-xs text-muted-foreground">
              (can't mix professional &amp; personal)
            </span>
          )}
        </div>
      )}

      <div className="space-y-3">
        <div>
          <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Professional
          </p>
          <CategoryButtons list={professional} />
        </div>
        <div>
          <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Personal
          </p>
          <CategoryButtons list={personal} />
        </div>
        {custom && (
          <Button
            type="button"
            size="sm"
            variant={activeCat === custom.id ? "default" : "outline"}
            onClick={() => setActiveCat(custom.id)}
          >
            {custom.label}
          </Button>
        )}
      </div>

      {isCustomScene ? (
        <div>
          <h4 className="mb-2 text-sm font-medium">Describe your scene</h4>
          <Textarea
            value={value.customPrompt}
            maxLength={400}
            onChange={(e) => setCustom(e.target.value)}
            placeholder="e.g. wearing a linen shirt, sitting at a sunlit café in Rome"
          />
          <p className="mt-1 text-xs text-muted-foreground">
            {value.customPrompt.length}/400 · we add your identity, lighting and quality
            automatically.
          </p>
        </div>
      ) : (
        category && (
          <div className="space-y-5">
            <OptionGrid
              kind="attire"
              options={category.attires}
              selected={value.attireRefs}
              limit={maxAttires}
            />
            <OptionGrid
              kind="background"
              options={category.backgrounds}
              selected={value.backgroundRefs}
              limit={maxBackgrounds}
            />
          </div>
        )
      )}

      {!isCustomScene && (
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
