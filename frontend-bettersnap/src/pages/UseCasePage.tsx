import { Link, useParams, Navigate } from "react-router-dom";
import { motion } from "framer-motion";
import { ArrowRight, ArrowLeft, Check, Camera, Sparkles } from "lucide-react";
import Navbar from "@/components/Navbar";
import PageShell from "@/components/PageShell";
import SocialProof from "@/components/SocialProof";
import { getUseCaseBySlug } from "@/data/useCases";
import logo from "@/assets/logo.jpeg";

const UseCasePage = () => {
  const { slug } = useParams<{ slug: string }>();
  const useCase = getUseCaseBySlug(slug);

  if (!useCase) return <Navigate to="/" replace />;

  const Icon = useCase.icon;

  return (
    <PageShell>
      <Navbar />

      <main className="pb-12">
        {/* Back link */}
        <div className="container mx-auto px-4 mb-6">
          <Link
            to="/"
            className="inline-flex items-center gap-1.5 text-sm font-medium text-muted-foreground hover:text-foreground transition-colors"
          >
            <ArrowLeft className="w-4 h-4" /> Back to home
          </Link>
        </div>

        {/* Hero */}
        <section className="container mx-auto px-4" aria-labelledby="usecase-heading">
          <div className="grid md:grid-cols-2 gap-10 items-center">
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.5 }}
            >
              <div className="inline-flex items-center gap-2 px-3 py-1.5 rounded-full bg-primary/10 border border-primary/20 text-xs font-medium text-primary mb-5 uppercase tracking-wide">
                <Icon className="w-3.5 h-3.5" /> {useCase.title}
              </div>
              <h1
                id="usecase-heading"
                className="text-4xl md:text-5xl font-heading font-bold text-foreground mb-5 leading-tight"
              >
                {useCase.heroHeading}
              </h1>
              <p className="text-lg text-muted-foreground mb-7 max-w-xl">
                {useCase.supportingText}
              </p>
              <div className="flex flex-wrap gap-3">
                <Link
                  to="/login"
                  className="inline-flex items-center gap-2 px-7 py-3.5 rounded-xl gradient-cta text-primary-foreground font-semibold hover-scale btn-glow shadow-lg"
                >
                  Try Your Headshot <ArrowRight className="w-4 h-4" />
                </Link>
                <Link
                  to="/"
                  className="inline-flex items-center gap-2 px-7 py-3.5 rounded-xl bg-white border border-border text-foreground font-semibold hover:bg-muted/50 transition-colors"
                >
                  Explore other use cases
                </Link>
              </div>
            </motion.div>

            {/* AI-generated example images (demo) */}
            <div className="grid grid-cols-2 gap-3">
              {useCase.exampleImages.map((img, i) => (
                <motion.figure
                  key={img.label}
                  initial={{ opacity: 0, scale: 0.95 }}
                  animate={{ opacity: 1, scale: 1 }}
                  transition={{ delay: 0.1 * i, duration: 0.4 }}
                  className="aspect-[4/5] rounded-2xl border border-glass-border relative overflow-hidden shadow-md bg-muted/40"
                >
                  <img
                    src={img.src}
                    alt={`${useCase.title} example: ${img.label}`}
                    loading={i < 2 ? "eager" : "lazy"}
                    width={768}
                    height={960}
                    className="absolute inset-0 w-full h-full object-cover"
                  />
                  <figcaption className="absolute bottom-3 left-3 right-3">
                    <span className="inline-block text-[10px] font-semibold uppercase tracking-wider px-2 py-1 rounded-md bg-white/90 text-foreground">
                      {img.label}
                    </span>
                  </figcaption>
                </motion.figure>
              ))}
            </div>
          </div>
          <p className="text-xs text-muted-foreground mt-4">
            Example previews are AI-generated demo images for illustration only.
          </p>
        </section>

        {/* How BetterSnap AI Helps */}
        <section className="container mx-auto px-4 py-20" aria-labelledby="helps-heading">
          <h2 id="helps-heading" className="text-2xl md:text-3xl font-heading font-bold text-foreground mb-8 text-center">
            How BetterSnap AI helps
          </h2>
          <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
            {useCase.benefits.map((b) => (
              <div key={b.title} className="glass-card rounded-2xl p-6">
                <div className="w-10 h-10 rounded-xl bg-primary/10 flex items-center justify-center mb-3">
                  <Sparkles className="w-5 h-5 text-primary" />
                </div>
                <h3 className="font-heading font-semibold text-foreground mb-1">{b.title}</h3>
                <p className="text-sm text-muted-foreground leading-relaxed">{b.desc}</p>
              </div>
            ))}
          </div>
        </section>

        {/* Recommended styles */}
        <section className="container mx-auto px-4 py-12" aria-labelledby="styles-heading">
          <h2 id="styles-heading" className="text-2xl md:text-3xl font-heading font-bold text-foreground mb-8 text-center">
            Recommended styles
          </h2>
          <div className="flex flex-wrap justify-center gap-3 max-w-3xl mx-auto">
            {useCase.recommendedStyles.map((s) => (
              <span
                key={s}
                className="px-4 py-2 rounded-full bg-white border border-border text-sm font-medium text-foreground shadow-sm"
              >
                {s}
              </span>
            ))}
          </div>
        </section>

        {/* Suggested photo guidance */}
        <section className="container mx-auto px-4 py-12" aria-labelledby="guidance-heading">
          <div className="max-w-3xl mx-auto glass-card rounded-2xl p-8">
            <div className="flex items-center gap-3 mb-5">
              <div className="w-10 h-10 rounded-xl bg-accent/15 flex items-center justify-center">
                <Camera className="w-5 h-5 text-accent" />
              </div>
              <h2 id="guidance-heading" className="text-2xl font-heading font-bold text-foreground">
                Suggested photo guidance
              </h2>
            </div>
            <ul className="space-y-3">
              {useCase.guidance.map((g) => (
                <li key={g} className="flex items-start gap-3 text-foreground">
                  <Check className="w-5 h-5 text-primary shrink-0 mt-0.5" />
                  <span>{g}</span>
                </li>
              ))}
            </ul>
            {useCase.disclaimer && (
              <p className="mt-6 text-xs text-muted-foreground border-t border-border pt-4">
                {useCase.disclaimer}
              </p>
            )}
          </div>
        </section>

        {/* CTA */}
        <section className="container mx-auto px-4 py-16">
          <div className="glass rounded-3xl p-10 md:p-14 text-center relative overflow-hidden">
            <div className="absolute inset-0 gradient-hero opacity-10 pointer-events-none" aria-hidden="true" />
            <div className="relative z-10">
              <h2 className="text-3xl md:text-4xl font-heading font-bold text-foreground mb-4">
                Ready to create your {useCase.title.toLowerCase()} photo?
              </h2>
              <Link
                to="/login"
                className="inline-flex items-center gap-2 px-8 py-4 rounded-xl gradient-cta text-primary-foreground font-semibold text-lg hover-scale btn-glow shadow-lg"
              >
                Try Your Headshot <ArrowRight className="w-5 h-5" />
              </Link>
            </div>
          </div>
        </section>

        {/* Testimonials (reuses landing carousel) */}
        <SocialProof />
      </main>

      {/* Footer */}
      <footer className="py-12 border-t border-border" role="contentinfo">
        <div className="container mx-auto px-4 flex flex-col md:flex-row items-center justify-between gap-4">
          <div className="flex items-center gap-2">
            <img src={logo} alt="BetterSnap AI" className="w-7 h-7 rounded-lg object-cover" />
            <span className="font-heading font-bold text-foreground">
              Better<span className="text-gradient">Snap</span> AI
            </span>
          </div>
          <div className="flex flex-col md:flex-row items-center gap-3 md:gap-6">
            <Link to="/privacy-policy" className="text-sm text-muted-foreground hover:text-foreground">
              Privacy Policy
            </Link>
            <Link to="/contact-support" className="text-sm text-muted-foreground hover:text-foreground">
              Contact Support
            </Link>
            <p className="text-sm text-muted-foreground">© 2026 BetterSnap AI.</p>
          </div>
        </div>
      </footer>
    </PageShell>
  );
};

export default UseCasePage;
