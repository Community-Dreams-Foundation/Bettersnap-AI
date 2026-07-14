import { motion } from "framer-motion";
import {
  Check,
  X,
  Zap,
  Sparkles,
  CalendarClock,
  PackageOpen,
  ImageIcon,
  Coins,
  Shirt,
  Layers,
  Palette,
  Boxes,
} from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { useAuth } from "@/contexts/AuthContext";
import { type PlanId, type PlanType } from "@/lib/planCheckout";
import { setPlanIntent } from "@/lib/planIntent";

interface PlanCard {
  id: PlanId;
  name: string;
  price: string;
  period: string;
  description: string;
  features: string[];
  cta: string;
  highlight: boolean;
  badge: string | null;
}

const monthlyPlans: PlanCard[] = [
  {
    id: "basic",
    name: "Basic",
    price: "$25",
    period: "/mo",
    description: "For simple professional headshot needs.",
    features: [
      "20 headshots per month",
      "100 monthly credits",
      "2 attires × 2 backgrounds",
      "Professional-only style selection",
    ],
    cta: "Choose Basic Monthly",
    highlight: false,
    badge: null,
  },
  {
    id: "pro",
    name: "Pro",
    price: "$45",
    period: "/mo",
    description: "For job seekers, creators, and active professionals.",
    features: [
      "40 headshots per month",
      "200 monthly credits",
      "3 attires × 3 backgrounds",
      "Mix professional + personal styles",
    ],
    cta: "Choose Pro Monthly",
    highlight: true,
    badge: "Most Popular",
  },
  {
    id: "expert",
    name: "Expert",
    price: "$65",
    period: "/mo",
    description: "For executives, consultants, and heavy users.",
    features: [
      "60 headshots per month",
      "300 monthly credits",
      "5 attires × 5 backgrounds",
      "Mix professional + personal styles",
    ],
    cta: "Choose Expert Monthly",
    highlight: false,
    badge: null,
  },
];

const oneTimePlans: PlanCard[] = [
  {
    id: "trial",
    name: "Trial",
    price: "Free",
    period: "",
    description: "Try BetterSnap AI before choosing a plan.",
    features: [
      "4 headshots",
      "2 attires × 2 backgrounds",
      "Professional-only style selection",
      "1 credit = 1 image",
    ],
    cta: "Start Free Trial",
    highlight: false,
    badge: null,
  },
  {
    id: "basic",
    name: "Basic Pack",
    price: "$35",
    period: " one-time",
    description: "For users who need a simple professional set.",
    features: [
      "30 headshots",
      "2 attires",
      "2 backgrounds",
      "Image pack model",
    ],
    cta: "Buy Basic Pack",
    highlight: false,
    badge: null,
  },
  {
    id: "pro",
    name: "Pro Pack",
    price: "$45",
    period: " one-time",
    description: "For job seekers and personal branding.",
    features: [
      "50 headshots",
      "3 attires",
      "3 backgrounds",
      "Mix professional + personal styles",
    ],
    cta: "Buy Pro Pack",
    highlight: true,
    badge: "Most Popular",
  },
  {
    id: "expert",
    name: "Expert Pack",
    price: "$65",
    period: " one-time",
    description: "For users who want the most variety.",
    features: [
      "70 headshots",
      "5 attires",
      "5 backgrounds",
      "Mix professional + personal styles",
    ],
    cta: "Buy Expert Pack",
    highlight: false,
    badge: null,
  },
];

type MonthlyRow = {
  plan: string;
  price: string;
  images: string;
  credits: string;
  attires: string;
  backgrounds: string;
  combos: string;
  mix: boolean;
  highlight?: boolean;
};

type OneTimeRow = {
  plan: string;
  price: string;
  images: string;
  attires: string;
  backgrounds: string;
  combos: string;
  mix: boolean;
  model: string;
  highlight?: boolean;
};

const monthlyTable: MonthlyRow[] = [
  { plan: "Basic", price: "$25/mo", images: "20", credits: "100", attires: "2", backgrounds: "2", combos: "2 × 2", mix: false },
  { plan: "Pro", price: "$45/mo", images: "40", credits: "200", attires: "3", backgrounds: "3", combos: "3 × 3", mix: true, highlight: true },
  { plan: "Expert", price: "$65/mo", images: "60", credits: "300", attires: "5", backgrounds: "5", combos: "5 × 5", mix: true },
];

const oneTimeTable: OneTimeRow[] = [
  { plan: "Trial", price: "Free", images: "4", attires: "2", backgrounds: "2", combos: "2 × 2", mix: false, model: "Image pack" },
  { plan: "Basic", price: "$35", images: "30", attires: "2", backgrounds: "2", combos: "2 × 2", mix: false, model: "Image pack" },
  { plan: "Pro", price: "$45", images: "50", attires: "3", backgrounds: "3", combos: "3 × 3", mix: true, model: "Image pack", highlight: true },
  { plan: "Expert", price: "$65", images: "70", attires: "5", backgrounds: "5", combos: "5 × 5", mix: true, model: "Image pack" },
];

const PlanPill = ({ name, highlight }: { name: string; highlight?: boolean }) => (
  <span
    className={`inline-flex items-center px-2.5 py-1 rounded-full text-xs font-semibold ${
      highlight
        ? "gradient-cta text-white shadow-sm"
        : "bg-primary/10 text-primary"
    }`}
  >
    {name}
  </span>
);

const YesNo = ({ v }: { v: boolean }) =>
  v ? (
    <span className="inline-flex items-center gap-1 text-primary font-medium">
      <Check className="w-4 h-4" /> Yes
    </span>
  ) : (
    <span className="inline-flex items-center gap-1 text-muted-foreground">
      <X className="w-4 h-4" /> No
    </span>
  );

const PricingSection = () => {
  const [tab, setTab] = useState<PlanType>("monthly");
  const [busy, setBusy] = useState<string | null>(null);
  const navigate = useNavigate();
  const { user } = useAuth();

  // TODO: Connect billing plan selection to Stripe Checkout once the approved checkout endpoint is available.
  const onSelect = async (planType: PlanType, id: PlanId) => {
    const key = `${planType}-${id}`;
    if (busy) return;
    setBusy(key);
    try {
      setPlanIntent({ planType, planId: id, source: "landing_pricing" });

      if (!user) {
        toast.info("Create an account or sign in to continue with your selected plan.");
        navigate("/signup");
        return;
      }

      if (id === "trial") {
        navigate("/onboarding");
        return;
      }

      navigate("/billing");
    } finally {
      setBusy(null);
    }
  };

  const plans = tab === "monthly" ? monthlyPlans : oneTimePlans;
  const gridCols =
    plans.length === 4 ? "md:grid-cols-2 lg:grid-cols-4" : "md:grid-cols-3";

  return (
    <section id="pricing" className="py-20 relative" aria-labelledby="pricing-heading">
      <div className="container mx-auto px-4">
        {/* Header */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          className="text-center mb-8 max-w-3xl mx-auto"
        >
          <h2
            id="pricing-heading"
            className="text-3xl md:text-4xl lg:text-5xl font-heading font-bold text-foreground mb-4"
          >
            Choose the plan that fits your <span className="text-gradient">headshot needs</span>
          </h2>
          <p className="text-muted-foreground text-base md:text-lg leading-relaxed">
            Start with a one-time image pack or choose a monthly plan for recurring
            professional headshots.
          </p>
          <div className="mt-4 flex flex-wrap items-center justify-center gap-2">
            <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-primary/10 text-primary text-xs font-semibold">
              <CalendarClock className="w-3.5 h-3.5" /> Monthly plans
            </span>
            <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-accent/10 text-accent-foreground text-xs font-semibold border border-border">
              <PackageOpen className="w-3.5 h-3.5" /> One-time packs
            </span>
          </div>
        </motion.div>

        {/* Segmented Tabs */}
        <div className="flex justify-center mb-8">
          <div
            role="tablist"
            aria-label="Pricing options"
            className="inline-flex p-1 rounded-full bg-white border border-border shadow-sm"
          >
            {(
              [
                { id: "monthly", label: "Monthly Plans", icon: CalendarClock },
                { id: "one_time", label: "One-Time Image Packs", icon: PackageOpen },
              ] as { id: PlanType; label: string; icon: typeof CalendarClock }[]
            ).map((t) => {
              const Icon = t.icon;
              const active = tab === t.id;
              return (
                <button
                  key={t.id}
                  role="tab"
                  aria-selected={active}
                  onClick={() => setTab(t.id)}
                  className={`inline-flex items-center gap-2 px-4 md:px-6 py-2.5 rounded-full text-sm font-semibold transition-all ${
                    active
                      ? "gradient-cta text-white shadow-md"
                      : "text-muted-foreground hover:text-foreground"
                  }`}
                >
                  <Icon className="w-4 h-4" />
                  {t.label}
                </button>
              );
            })}
          </div>
        </div>

        {/* Cards */}
        <div className={`grid gap-6 lg:gap-7 items-stretch ${gridCols}`}>
          {plans.map((plan, i) => (
            <motion.div
              key={`${tab}-${plan.id}`}
              initial={{ opacity: 0, y: 20 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true }}
              transition={{ delay: i * 0.06 }}
              className={`group relative flex flex-col rounded-3xl border bg-white transition-all duration-300 ${
                plan.highlight
                  ? "border-primary/40 shadow-xl shadow-primary/10 md:scale-[1.02]"
                  : "border-border shadow-md hover:shadow-xl hover:border-primary/30"
              }`}
            >
              {plan.badge && (
                <div className="absolute -top-4 left-1/2 -translate-x-1/2">
                  <span className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-full gradient-cta text-white text-xs font-semibold shadow-md whitespace-nowrap">
                    <Zap className="w-3.5 h-3.5" aria-hidden="true" />
                    {plan.badge}
                  </span>
                </div>
              )}

              <div className="p-7 flex flex-col flex-1">
                <h3 className="font-heading font-semibold text-xl text-foreground mb-2">
                  {plan.name}
                </h3>

                <div className="mb-3">
                  <span className="text-4xl md:text-5xl font-heading font-bold text-foreground tracking-tight">
                    {plan.price}
                  </span>
                  {plan.period && (
                    <span className="text-muted-foreground text-sm font-medium ml-1">
                      {plan.period}
                    </span>
                  )}
                </div>

                <p className="text-muted-foreground text-sm leading-relaxed mb-5 min-h-[2.5rem]">
                  {plan.description}
                </p>

                <div className="h-px bg-border mb-5" />

                <ul className="space-y-3 mb-7 flex-1">
                  {plan.features.map((feature) => (
                    <li key={feature} className="flex items-start gap-3">
                      <span
                        className={`mt-0.5 inline-flex items-center justify-center w-5 h-5 rounded-full shrink-0 ${
                          plan.highlight
                            ? "bg-primary/10 text-primary"
                            : "bg-muted text-muted-foreground"
                        }`}
                      >
                        <Check className="w-3 h-3" aria-hidden="true" />
                      </span>
                      <span className="text-sm text-foreground/90 leading-relaxed">
                        {feature}
                      </span>
                    </li>
                  ))}
                </ul>

                <button
                  type="button"
                  onClick={() => onSelect(tab, plan.id)}
                  disabled={busy === `${tab}-${plan.id}`}
                  className={`block w-full text-center py-3.5 rounded-xl font-semibold text-sm transition-all duration-300 gradient-cta text-white shadow-md hover:shadow-lg hover-scale ${
                    busy === `${tab}-${plan.id}` ? "opacity-70 cursor-wait" : ""
                  }`}
                >
                  {busy === `${tab}-${plan.id}` ? "Please wait…" : plan.cta}
                </button>
              </div>
            </motion.div>
          ))}
        </div>

        {/* Comparison */}
        <div className="mt-14 max-w-6xl mx-auto">
          <div className="text-center mb-6">
            <h3 className="font-heading font-semibold text-2xl md:text-3xl text-foreground">
              {tab === "monthly" ? "Monthly plans compared" : "One-time image packs compared"}
            </h3>
            <p className="text-muted-foreground text-sm mt-1">
              A side-by-side look at what each plan includes.
            </p>
          </div>

          {/* Desktop table */}
          <div className="hidden md:block rounded-3xl border border-border bg-white/90 backdrop-blur shadow-lg overflow-hidden">
            <div className="overflow-x-auto">
              {tab === "monthly" ? (
                <table className="w-full text-sm">
                  <thead className="bg-gradient-to-r from-primary/10 via-accent/10 to-primary/5 text-foreground">
                    <tr>
                      {[
                        { label: "Plan", icon: null },
                        { label: "Monthly price", icon: null },
                        { label: "Headshots/mo", icon: ImageIcon },
                        { label: "Monthly credits", icon: Coins },
                        { label: "Attires", icon: Shirt },
                        { label: "Backgrounds", icon: ImageIcon },
                        { label: "Style combinations", icon: Layers },
                        { label: "Mix pro + personal", icon: Palette },
                      ].map((h) => {
                        const Icon = h.icon;
                        return (
                          <th
                            key={h.label}
                            className="text-left font-semibold px-5 py-4 whitespace-nowrap"
                          >
                            <span className="inline-flex items-center gap-2">
                              {Icon && <Icon className="w-4 h-4 text-primary" />}
                              {h.label}
                            </span>
                          </th>
                        );
                      })}
                    </tr>
                  </thead>
                  <tbody>
                    {monthlyTable.map((row, idx) => (
                      <tr
                        key={row.plan}
                        className={`border-t border-border transition-colors hover:bg-primary/[0.03] ${
                          row.highlight
                            ? "bg-primary/[0.06]"
                            : idx % 2 === 1
                            ? "bg-muted/30"
                            : ""
                        }`}
                      >
                        <td className="px-5 py-4">
                          <PlanPill name={row.plan} highlight={row.highlight} />
                        </td>
                        <td className="px-5 py-4 font-semibold text-foreground">{row.price}</td>
                        <td className="px-5 py-4 text-foreground">{row.images}</td>
                        <td className="px-5 py-4 text-foreground">{row.credits}</td>
                        <td className="px-5 py-4 text-foreground">{row.attires}</td>
                        <td className="px-5 py-4 text-foreground">{row.backgrounds}</td>
                        <td className="px-5 py-4 text-foreground">{row.combos}</td>
                        <td className="px-5 py-4"><YesNo v={row.mix} /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <table className="w-full text-sm">
                  <thead className="bg-gradient-to-r from-primary/10 via-accent/10 to-primary/5 text-foreground">
                    <tr>
                      {[
                        { label: "Plan", icon: null },
                        { label: "Pack price", icon: null },
                        { label: "Total headshots", icon: ImageIcon },
                        { label: "Attires", icon: Shirt },
                        { label: "Backgrounds", icon: ImageIcon },
                        { label: "Style combinations", icon: Layers },
                        { label: "Mix pro + personal", icon: Palette },
                        { label: "Model type", icon: Boxes },
                      ].map((h) => {
                        const Icon = h.icon;
                        return (
                          <th
                            key={h.label}
                            className="text-left font-semibold px-5 py-4 whitespace-nowrap"
                          >
                            <span className="inline-flex items-center gap-2">
                              {Icon && <Icon className="w-4 h-4 text-primary" />}
                              {h.label}
                            </span>
                          </th>
                        );
                      })}
                    </tr>
                  </thead>
                  <tbody>
                    {oneTimeTable.map((row, idx) => (
                      <tr
                        key={row.plan}
                        className={`border-t border-border transition-colors hover:bg-primary/[0.03] ${
                          row.highlight
                            ? "bg-primary/[0.06]"
                            : idx % 2 === 1
                            ? "bg-muted/30"
                            : ""
                        }`}
                      >
                        <td className="px-5 py-4">
                          <PlanPill name={row.plan} highlight={row.highlight} />
                        </td>
                        <td className="px-5 py-4 font-semibold text-foreground">{row.price}</td>
                        <td className="px-5 py-4 text-foreground">{row.images}</td>
                        <td className="px-5 py-4 text-foreground">{row.attires}</td>
                        <td className="px-5 py-4 text-foreground">{row.backgrounds}</td>
                        <td className="px-5 py-4 text-foreground">{row.combos}</td>
                        <td className="px-5 py-4"><YesNo v={row.mix} /></td>
                        <td className="px-5 py-4 text-muted-foreground">{row.model}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>

          {/* Mobile comparison cards */}
          <div className="md:hidden space-y-4">
            {tab === "monthly"
              ? monthlyTable.map((row) => (
                  <div
                    key={row.plan}
                    className={`rounded-2xl border p-5 bg-white shadow-sm ${
                      row.highlight ? "border-primary/40 shadow-primary/10" : "border-border"
                    }`}
                  >
                    <div className="flex items-center justify-between mb-3">
                      <PlanPill name={row.plan} highlight={row.highlight} />
                      <span className="font-semibold text-foreground">{row.price}</span>
                    </div>
                    <dl className="grid grid-cols-2 gap-y-2 text-sm">
                      <dt className="text-muted-foreground">Headshots/mo</dt>
                      <dd className="text-right text-foreground">{row.images}</dd>
                      <dt className="text-muted-foreground">Monthly credits</dt>
                      <dd className="text-right text-foreground">{row.credits}</dd>
                      <dt className="text-muted-foreground">Attires</dt>
                      <dd className="text-right text-foreground">{row.attires}</dd>
                      <dt className="text-muted-foreground">Backgrounds</dt>
                      <dd className="text-right text-foreground">{row.backgrounds}</dd>
                      <dt className="text-muted-foreground">Style combinations</dt>
                      <dd className="text-right text-foreground">{row.combos}</dd>
                      <dt className="text-muted-foreground">Mix pro + personal</dt>
                      <dd className="text-right"><YesNo v={row.mix} /></dd>
                    </dl>
                  </div>
                ))
              : oneTimeTable.map((row) => (
                  <div
                    key={row.plan}
                    className={`rounded-2xl border p-5 bg-white shadow-sm ${
                      row.highlight ? "border-primary/40 shadow-primary/10" : "border-border"
                    }`}
                  >
                    <div className="flex items-center justify-between mb-3">
                      <PlanPill name={row.plan} highlight={row.highlight} />
                      <span className="font-semibold text-foreground">{row.price}</span>
                    </div>
                    <dl className="grid grid-cols-2 gap-y-2 text-sm">
                      <dt className="text-muted-foreground">Total headshots</dt>
                      <dd className="text-right text-foreground">{row.images}</dd>
                      <dt className="text-muted-foreground">Attires</dt>
                      <dd className="text-right text-foreground">{row.attires}</dd>
                      <dt className="text-muted-foreground">Backgrounds</dt>
                      <dd className="text-right text-foreground">{row.backgrounds}</dd>
                      <dt className="text-muted-foreground">Style combinations</dt>
                      <dd className="text-right text-foreground">{row.combos}</dd>
                      <dt className="text-muted-foreground">Mix pro + personal</dt>
                      <dd className="text-right"><YesNo v={row.mix} /></dd>
                      <dt className="text-muted-foreground">Model type</dt>
                      <dd className="text-right text-foreground">{row.model}</dd>
                    </dl>
                  </div>
                ))}
          </div>
        </div>

        {/* Info cards */}
        <div className="mt-10 max-w-4xl mx-auto grid gap-4 md:grid-cols-2">
          <div className="rounded-2xl border border-border bg-white p-5 shadow-sm">
            <div className="flex items-center gap-2 mb-2">
              <span className="inline-flex items-center justify-center w-8 h-8 rounded-lg bg-primary/10 text-primary">
                <CalendarClock className="w-4 h-4" />
              </span>
              <h4 className="font-heading font-semibold text-foreground">Monthly plans</h4>
            </div>
            <p className="text-sm text-muted-foreground leading-relaxed">
              Best for users who generate headshots regularly and want recurring credits each month.
            </p>
          </div>
          <div className="rounded-2xl border border-border bg-white p-5 shadow-sm">
            <div className="flex items-center gap-2 mb-2">
              <span className="inline-flex items-center justify-center w-8 h-8 rounded-lg bg-accent/10 text-primary">
                <PackageOpen className="w-4 h-4" />
              </span>
              <h4 className="font-heading font-semibold text-foreground">One-time image packs</h4>
            </div>
            <p className="text-sm text-muted-foreground leading-relaxed">
              Best for users who want a fixed number of headshots without a subscription.
            </p>
          </div>
        </div>

        <p className="mt-8 text-center text-xs text-muted-foreground/70 max-w-2xl mx-auto flex items-center justify-center gap-1.5">
          <Sparkles className="w-3.5 h-3.5" />
          Payments are being finalized. Plan selection buttons are prepared for Stripe checkout.
        </p>
      </div>
    </section>
  );
};

export default PricingSection;
