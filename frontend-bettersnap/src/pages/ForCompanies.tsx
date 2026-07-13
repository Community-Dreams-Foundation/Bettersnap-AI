import { Link } from "react-router-dom";
import { ArrowRight, Sparkles, Palette, Users, Zap, ShieldCheck, Building2, UserPlus, Camera } from "lucide-react";
import Navbar from "@/components/Navbar";
import PageShell from "@/components/PageShell";
import Footer from "@/components/Footer";
import { motion } from "framer-motion";

const stats = [
  { value: "500+", label: "Teams" },
  { value: "10,000+", label: "Employees" },
  { value: "<30 min", label: "Per Person" },
  { value: "99%", label: "Satisfaction" },
];

const features = [
  {
    icon: Palette,
    title: "Brand Consistency",
    description: "Every headshot follows your brand guidelines — backgrounds, lighting, and style.",
  },
  {
    icon: Users,
    title: "Bulk Management",
    description: "Invite hundreds of employees at once. Track progress from a single dashboard.",
  },
  {
    icon: Zap,
    title: "Fast Delivery",
    description: "Each team member gets their headshots in under 30 minutes — no studio needed.",
  },
  {
    icon: ShieldCheck,
    title: "Secure & Compliant",
    description: "Enterprise-grade security with GDPR-compliant data handling and storage.",
  },
];

const steps = [
  { icon: Building2, title: "Create your org", description: "Set up your company workspace and brand guidelines in minutes." },
  { icon: UserPlus, title: "Invite your team", description: "Send invites by email or upload a roster. No accounts to manage." },
  { icon: Camera, title: "Everyone gets headshots", description: "Each member uploads a selfie and receives professional headshots." },
];

const ForCompanies = () => {
  const scrollToHow = () => {
    const el = document.getElementById("how-it-works");
    if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <PageShell>
      <Navbar />

      {/* Hero */}
      <section className="pt-32 pb-20" aria-labelledby="hero-heading">
        <div className="container mx-auto px-4 text-center">
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5 }}
            className="inline-flex items-center gap-2 px-4 py-2 rounded-full glass text-sm font-medium text-foreground mb-6"
          >
            <Sparkles className="w-3.5 h-3.5 text-accent" aria-hidden="true" />
            For companies & teams
          </motion.div>
          <motion.h1
            id="hero-heading"
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, delay: 0.1 }}
            className="text-4xl md:text-6xl font-heading font-bold text-foreground mb-6 max-w-4xl mx-auto"
          >
            Amazing headshots for <span className="text-gradient">companies and teams</span>
          </motion.h1>
          <motion.p
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, delay: 0.2 }}
            className="text-lg md:text-xl text-muted-foreground mb-10 max-w-2xl mx-auto"
          >
            Consistent, professional, on-brand headshots for entire organizations — in minutes, not weeks.
          </motion.p>
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.5, delay: 0.3 }}
            className="flex flex-col sm:flex-row items-center justify-center gap-4"
          >
            <Link
              to="/org/onboarding"
              className="inline-flex items-center gap-2 px-8 py-4 rounded-xl gradient-cta text-primary-foreground font-semibold text-lg hover-scale btn-glow shadow-lg"
            >
              Create Your Team
              <ArrowRight className="w-5 h-5" aria-hidden="true" />
            </Link>
            <button
              onClick={scrollToHow}
              className="inline-flex items-center gap-2 px-8 py-4 rounded-xl glass text-foreground font-semibold text-lg hover-scale"
            >
              See How It Works
            </button>
          </motion.div>
        </div>
      </section>

      {/* Stats */}
      <section className="relative" aria-label="Company metrics">
        <div className="container mx-auto px-4">
          <div className="border-t border-border" aria-hidden="true" />
          <div className="py-16">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-8 md:gap-0">
              {stats.map((s, i) => (
                <div
                  key={s.label}
                  className={`flex flex-col items-center text-center ${
                    i < stats.length - 1 ? "md:border-r md:border-border" : ""
                  }`}
                >
                  <span className="text-4xl md:text-5xl font-heading font-bold text-foreground mb-2">{s.value}</span>
                  <span className="text-sm md:text-base text-muted-foreground font-medium">{s.label}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>

      {/* Features */}
      <section className="py-24" aria-labelledby="features-heading">
        <div className="container mx-auto px-4">
          <div className="text-center mb-16">
            <h2 id="features-heading" className="text-3xl md:text-4xl font-heading font-bold text-foreground mb-4">
              Built for <span className="text-gradient">growing teams</span>
            </h2>
            <p className="text-muted-foreground text-lg max-w-2xl mx-auto">
              Everything your organization needs to look polished and professional.
            </p>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
            {features.map((f, i) => (
              <motion.div
                key={f.title}
                initial={{ opacity: 0, y: 20 }}
                whileInView={{ opacity: 1, y: 0 }}
                viewport={{ once: true }}
                transition={{ duration: 0.4, delay: i * 0.1 }}
                className="glass-card rounded-2xl p-6"
              >
                <div className="w-12 h-12 rounded-xl gradient-cta flex items-center justify-center mb-4">
                  <f.icon className="w-6 h-6 text-primary-foreground" aria-hidden="true" />
                </div>
                <h3 className="text-lg font-heading font-semibold text-foreground mb-2">{f.title}</h3>
                <p className="text-sm text-muted-foreground leading-relaxed">{f.description}</p>
              </motion.div>
            ))}
          </div>
        </div>
      </section>

      {/* How it works */}
      <section id="how-it-works" className="py-24" aria-labelledby="how-heading">
        <div className="container mx-auto px-4">
          <div className="text-center mb-16">
            <h2 id="how-heading" className="text-3xl md:text-4xl font-heading font-bold text-foreground mb-4">
              How it <span className="text-gradient">works</span>
            </h2>
            <p className="text-muted-foreground text-lg max-w-2xl mx-auto">
              Three simple steps to professional headshots for your whole team.
            </p>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-8 max-w-5xl mx-auto">
            {steps.map((step, i) => (
              <motion.div
                key={step.title}
                initial={{ opacity: 0, y: 20 }}
                whileInView={{ opacity: 1, y: 0 }}
                viewport={{ once: true }}
                transition={{ duration: 0.4, delay: i * 0.15 }}
                className="glass rounded-2xl p-8 text-center relative"
              >
                <div className="absolute -top-4 left-1/2 -translate-x-1/2 w-10 h-10 rounded-full gradient-cta text-primary-foreground font-heading font-bold flex items-center justify-center shadow-lg">
                  {i + 1}
                </div>
                <div className="w-16 h-16 rounded-2xl bg-secondary flex items-center justify-center mx-auto mt-2 mb-5">
                  <step.icon className="w-8 h-8 text-primary" aria-hidden="true" />
                </div>
                <h3 className="text-xl font-heading font-semibold text-foreground mb-2">{step.title}</h3>
                <p className="text-sm text-muted-foreground leading-relaxed">{step.description}</p>
              </motion.div>
            ))}
          </div>
        </div>
      </section>

      {/* Final CTA */}
      <section className="py-24" aria-labelledby="cta-heading">
        <div className="container mx-auto px-4">
          <div className="glass rounded-3xl p-12 md:p-16 text-center relative overflow-hidden">
            <div className="absolute inset-0 gradient-hero opacity-10 pointer-events-none" aria-hidden="true" />
            <div
              className="absolute top-10 left-10 w-32 h-32 bg-primary/20 rounded-full blur-3xl pointer-events-none"
              aria-hidden="true"
            />
            <div
              className="absolute bottom-10 right-10 w-40 h-40 bg-accent/15 rounded-full blur-3xl pointer-events-none"
              aria-hidden="true"
            />
            <div className="relative z-10">
              <div className="inline-flex items-center gap-2 px-4 py-2 rounded-full glass text-sm font-medium text-foreground mb-6">
                <Sparkles className="w-3.5 h-3.5 text-accent" aria-hidden="true" />
                Get your team started today
              </div>
              <h2 id="cta-heading" className="text-3xl md:text-4xl font-heading font-bold text-foreground mb-4">
                Ready to give your team perfect headshots?
              </h2>
              <p className="text-muted-foreground text-lg mb-8 max-w-md mx-auto">
                Join hundreds of companies upgrading their team's professional image with BetterSnap AI.
              </p>
              <Link
                to="/org/onboarding"
                className="inline-flex items-center gap-2 px-8 py-4 rounded-xl gradient-cta text-primary-foreground font-semibold text-lg hover-scale btn-glow shadow-lg"
              >
                Create Your Team
                <ArrowRight className="w-5 h-5" aria-hidden="true" />
              </Link>
            </div>
          </div>
        </div>
      </section>

      <Footer />
    </PageShell>
  );
};

export default ForCompanies;
