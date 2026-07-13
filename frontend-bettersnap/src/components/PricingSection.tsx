import { motion } from "framer-motion";
import { Check, Zap } from "lucide-react";
import { Link } from "react-router-dom";

const plans = [
  {
    name: "SnapPass Monthly",
    price: "$12",
    period: "/month",
    description: "Best for users who want fresh headshots, profile updates, and ongoing access.",
    features: [
      "25 AI headshot generations per month",
      "Access to professional and personal styles",
      "LinkedIn, resume, university ID, and travel photo styles",
      "Priority generation queue",
      "Monthly refresh credits",
    ],
    cta: "Start Monthly",
    ctaLink: "/onboarding",
    highlight: false,
    badge: null,
  },
  {
    name: "Single Session",
    price: "$29",
    period: " one-time",
    description: "Best for users who need a complete professional headshot set without a subscription.",
    features: [
      "60 AI headshot generations",
      "Multiple background and outfit styles",
      "Professional, student, and personal use cases",
      "High-resolution downloads",
      "No monthly commitment",
    ],
    cta: "Create Headshots",
    ctaLink: "/onboarding",
    highlight: true,
    badge: "Most Popular",
  },
  {
    name: "Flex Credits",
    price: "$9",
    period: " starter",
    description: "Best for users who want to pay only when they generate photos.",
    features: [
      "Buy credits when needed",
      "10 credits included in starter pack",
      "Use credits for headshots and style variations",
      "No expiration during beta",
      "Ideal for occasional users",
    ],
    cta: "Buy Credits",
    ctaLink: "/onboarding",
    highlight: false,
    badge: null,
  },
];

const PricingSection = () => {
  return (
    <section id="pricing" className="py-24 relative" aria-labelledby="pricing-heading">
      <div className="container mx-auto px-4">
        {/* Header */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          className="text-center mb-16 max-w-3xl mx-auto"
        >
          <h2
            id="pricing-heading"
            className="text-3xl md:text-4xl lg:text-5xl font-heading font-bold text-foreground mb-5"
          >
            Choose the plan that fits your{" "}
            <span className="text-gradient">headshot needs</span>
          </h2>
          <p className="text-muted-foreground text-lg leading-relaxed">
            Flexible options for students, job seekers, professionals, and teams. Start with a
            one-time package, subscribe monthly, or buy credits whenever you need them.
          </p>
        </motion.div>

        {/* Cards */}
        <div className="grid md:grid-cols-3 gap-6 lg:gap-8 items-stretch">
          {plans.map((plan, i) => (
            <motion.div
              key={plan.name}
              initial={{ opacity: 0, y: 20 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true }}
              transition={{ delay: i * 0.1 }}
              className={`relative flex flex-col rounded-3xl border transition-all duration-300 ${
                plan.highlight
                  ? "border-primary/40 bg-white shadow-xl shadow-primary/10 scale-[1.02] md:scale-[1.03]"
                  : "border-border bg-white/80 shadow-lg hover:shadow-xl"
              }`}
            >
              {/* Badge */}
              {plan.badge && (
                <div className="absolute -top-4 left-1/2 -translate-x-1/2">
                  <span className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded-full bg-primary text-white text-xs font-semibold shadow-md">
                    <Zap className="w-3.5 h-3.5" aria-hidden="true" />
                    {plan.badge}
                  </span>
                </div>
              )}

              {/* Glow for highlighted card */}
              {plan.highlight && (
                <div
                  className="absolute -inset-px rounded-3xl pointer-events-none"
                  style={{
                    background: "linear-gradient(135deg, hsl(245 80% 60% / 0.15) 0%, hsl(265 75% 65% / 0.1) 100%)",
                    borderRadius: "1.5rem",
                    zIndex: -1,
                  }}
                  aria-hidden="true"
                />
              )}

              <div className="p-8 flex flex-col flex-1">
                {/* Plan name */}
                <h3 className="font-heading font-semibold text-xl text-foreground mb-2">
                  {plan.name}
                </h3>

                {/* Price */}
                <div className="mb-4">
                  <span className="text-4xl md:text-5xl font-heading font-bold text-foreground tracking-tight">
                    {plan.price}
                  </span>
                  <span className="text-muted-foreground text-sm font-medium ml-1">
                    {plan.period}
                  </span>
                </div>

                {/* Description */}
                <p className="text-muted-foreground text-sm leading-relaxed mb-6">
                  {plan.description}
                </p>

                {/* Divider */}
                <div className="h-px bg-border mb-6" />

                {/* Features */}
                <ul className="space-y-3 mb-8 flex-1">
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

                {/* CTA */}
                <Link
                  to={plan.ctaLink}
                  className={`block w-full text-center py-3.5 rounded-xl font-semibold text-sm transition-all duration-300 ${
                    plan.highlight
                      ? "gradient-cta text-white shadow-lg hover:shadow-xl hover-scale"
                      : "bg-muted text-foreground hover:bg-muted/80 border border-border"
                  }`}
                >
                  {plan.cta}
                </Link>
              </div>
            </motion.div>
          ))}
        </div>

        {/* Disclaimer */}
        <motion.p
          initial={{ opacity: 0 }}
          whileInView={{ opacity: 1 }}
          viewport={{ once: true }}
          className="text-center text-xs text-muted-foreground/70 mt-10 max-w-lg mx-auto"
        >
          Pricing shown is for early access and may change as BetterSnap AI expands.
        </motion.p>
      </div>
    </section>
  );
};

export default PricingSection;
