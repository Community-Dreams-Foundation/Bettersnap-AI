import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import PageShell from "@/components/PageShell";
import Navbar from "@/components/Navbar";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Loader2, Check, Sparkles } from "lucide-react";
import { toast } from "sonner";
import {
  getSubscriptionPlans,
  getSubscriptionStatus,
  createCheckout,
  type PaymentType,
  type OneTimePlanInfo,
  type MonthlyPlanInfo,
} from "@/lib/azureApi";
import { clearPlanIntent, getPlanIntent, type PlanIntent } from "@/lib/planIntent";

const TIER_META: Record<string, { name: string; blurb: string }> = {
  trial: { name: "Trial", blurb: "A free taste of BetterSnap AI" },
  basic: { name: "Basic", blurb: "A quick set of professional headshots" },
  pro: { name: "Pro", blurb: "More looks — mix professional & personal" },
  expert: { name: "Expert", blurb: "The full range, maximum variety" },
};

const dollars = (cents: number) => `$${(cents / 100).toFixed(cents % 100 === 0 ? 0 : 2)}`;

interface Status {
  subscription_plan: string | null;
  subscription_type: string | null;
  credits_remaining: number;
}

const Billing = () => {
  const [params, setParams] = useSearchParams();
  // Preselect the tab from the landing-page plan intent, if any.
  const initialIntent = useMemo<PlanIntent | null>(() => getPlanIntent(), []);
  const [billing, setBilling] = useState<PaymentType>(
    initialIntent?.planType ?? "one_time",
  );
  const [oneTime, setOneTime] = useState<OneTimePlanInfo[]>([]);
  const [monthly, setMonthly] = useState<MonthlyPlanInfo[]>([]);
  const [status, setStatus] = useState<Status | null>(null);
  const [loading, setLoading] = useState(true);
  const [buying, setBuying] = useState<string | null>(null);
  const highlightedPlan = initialIntent?.planId ?? null;

  // Toast + clear the ?checkout=success|cancel returned by Stripe.
  useEffect(() => {
    const c = params.get("checkout");
    if (c === "success") {
      toast.success("Payment complete — your credits have been added!");
      // Selected plan flow is complete — safe to clear the saved intent.
      clearPlanIntent();
    } else if (c === "cancel") {
      toast.info("Checkout canceled.");
    }
    if (c) {
      params.delete("checkout");
      setParams(params, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const [plans, st] = await Promise.all([
          getSubscriptionPlans(),
          getSubscriptionStatus().catch(() => null),
        ]);
        setOneTime(plans.one_time ?? []);
        setMonthly(plans.monthly ?? []);
        setStatus(st);
      } catch (e: any) {
        toast.error("Couldn't load plans", { description: e?.message });
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  // TODO: Connect billing plan selection to Stripe Checkout once the approved
  // checkout endpoint is available. `createCheckout` is the existing hook —
  // if it returns a URL we hand off; otherwise we surface a friendly message
  // and keep the selected-plan intent so the user can retry.
  const buy = async (plan: string) => {
    try {
      setBuying(plan);
      const res = await createCheckout(plan, billing);
      const url = res?.checkout_url;
      if (url && typeof url === "string") {
        window.location.href = url; // hand off to Stripe Checkout
        return;
      }
      toast.info(
        "Checkout is coming soon. Stripe payment integration is being finalized.",
      );
      setBuying(null);
    } catch (e: any) {
      toast.info(
        "Checkout is coming soon. Stripe payment integration is being finalized.",
        { description: e?.message },
      );
      setBuying(null);
    }
  };

  const nice = (p: string) => (TIER_META[p]?.name ?? p);
  const cards =
    billing === "one_time"
      ? oneTime.map((p) => ({
          plan: p.plan,
          images: p.images,
          price: dollars(p.discounted_cents),
          was: p.discounted_cents < p.original_cents ? dollars(p.original_cents) : null,
          suffix: "",
          cta: p.plan === "trial" ? "Start Free Trial" : `Buy ${nice(p.plan)} Pack`,
        }))
      : monthly.map((p) => ({
          plan: p.plan,
          images: p.images,
          price: dollars(p.price_cents),
          was: null as string | null,
          suffix: "/mo",
          cta: `Continue with ${nice(p.plan)} Monthly`,
        }));

  return (
    <PageShell>
      <Navbar />
      <main className="max-w-5xl mx-auto px-4 sm:px-6 lg:px-8 pt-28 pb-20">
        <header className="mb-8 text-center">
          <h1 className="text-4xl font-bold text-foreground">Plans &amp; Billing</h1>
          <p className="text-muted-foreground mt-2">Buy a one-time pack or subscribe monthly.</p>
        </header>

        {/* Current plan / credits */}
        {status && (
          <section className="mb-8 rounded-2xl border border-border bg-card p-5 flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-3">
              <span className="text-sm text-muted-foreground">Current plan</span>
              <Badge variant="secondary" className="capitalize">
                {status.subscription_plan || "Free"}
                {status.subscription_type ? ` · ${status.subscription_type.replace("_", "-")}` : ""}
              </Badge>
            </div>
            <div className="text-sm">
              <span className="font-semibold text-foreground">{status.credits_remaining}</span>
              <span className="text-muted-foreground"> credits remaining</span>
            </div>
          </section>
        )}

        {/* Billing toggle */}
        <div className="mb-8 flex justify-center">
          <div className="inline-flex rounded-full border border-border bg-secondary p-1">
            {(["one_time", "monthly"] as PaymentType[]).map((t) => (
              <button
                key={t}
                onClick={() => setBilling(t)}
                className={[
                  "rounded-full px-5 py-1.5 text-sm font-medium transition",
                  billing === t ? "bg-primary text-primary-foreground shadow" : "text-muted-foreground hover:text-foreground",
                ].join(" ")}
              >
                {t === "one_time" ? "One-time" : "Monthly"}
              </button>
            ))}
          </div>
        </div>

        {loading ? (
          <div className="flex justify-center py-16 text-muted-foreground">
            <Loader2 className="h-6 w-6 animate-spin" />
          </div>
        ) : (
          <div className="grid gap-6 md:grid-cols-3">
            {cards.map((c) => {
              const meta = TIER_META[c.plan] ?? { name: c.plan, blurb: "" };
              const isPopular = c.plan === "pro";
              const isSelected =
                highlightedPlan === c.plan &&
                initialIntent?.planType === billing;
              const highlight = isPopular || isSelected;
              return (
                <div
                  key={c.plan}
                  className={[
                    "relative flex flex-col rounded-3xl border p-6 transition-all",
                    isSelected
                      ? "border-primary ring-2 ring-primary/40 shadow-[0_8px_30px_hsl(245_80%_60%/0.25)]"
                      : highlight
                      ? "border-primary shadow-[0_8px_30px_hsl(245_80%_60%/0.18)]"
                      : "border-border",
                    "bg-card",
                  ].join(" ")}
                >
                  {isSelected && (
                    <span className="absolute -top-3 right-4 rounded-full bg-primary px-3 py-0.5 text-xs font-semibold text-primary-foreground shadow">
                      Selected
                    </span>
                  )}
                  {isPopular && !isSelected && (
                    <span className="absolute -top-3 left-1/2 -translate-x-1/2 rounded-full bg-primary px-3 py-0.5 text-xs font-semibold text-primary-foreground">
                      Most popular
                    </span>
                  )}
                  <h3 className="text-lg font-bold text-foreground">{meta.name}</h3>
                  <p className="mt-1 text-sm text-muted-foreground">{meta.blurb}</p>
                  <div className="mt-4 flex items-end gap-2">
                    <span className="text-3xl font-bold text-foreground">{c.price}</span>
                    <span className="text-sm text-muted-foreground">{c.suffix}</span>
                    {c.was && <span className="text-sm text-muted-foreground line-through">{c.was}</span>}
                  </div>
                  <ul className="mt-5 space-y-2 text-sm text-foreground">
                    <li className="flex items-center gap-2">
                      <Check className="h-4 w-4 text-primary" /> {c.images} AI headshots
                    </li>
                    <li className="flex items-center gap-2">
                      <Check className="h-4 w-4 text-primary" /> Your personal trained model
                    </li>
                    <li className="flex items-center gap-2">
                      <Check className="h-4 w-4 text-primary" /> 2K high-res downloads
                    </li>
                  </ul>
                  <Button
                    onClick={() => buy(c.plan)}
                    disabled={buying !== null}
                    className="mt-6 w-full gradient-cta font-semibold text-primary-foreground"
                  >
                    {buying === c.plan ? (
                      <>
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Redirecting…
                      </>
                    ) : (
                      <>
                        <Sparkles className="mr-2 h-4 w-4" /> {c.cta}
                      </>
                    )}
                  </Button>
                </div>
              );
            })}
          </div>
        )}
      </main>
    </PageShell>
  );
};

export default Billing;
