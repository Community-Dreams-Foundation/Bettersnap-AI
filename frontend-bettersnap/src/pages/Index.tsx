import Navbar from "@/components/Navbar";
import HeroSection from "@/components/HeroSection";
import FeaturesSection from "@/components/FeaturesSection";
import MetricsSection from "@/components/MetricsSection";
import HowItWorks from "@/components/HowItWorks";
import PricingSection from "@/components/PricingSection";
import UseCases from "@/components/UseCases";
import FAQSection from "@/components/FAQSection";
import SocialProof from "@/components/SocialProof";
import PageShell from "@/components/PageShell";
import Footer from "@/components/Footer";
import { Link, useLocation } from "react-router-dom";
import { ArrowRight, Sparkles } from "lucide-react";
import { useEffect } from "react";


const Index = () => {
  const location = useLocation();
  useEffect(() => {
    if (!location.hash) return;
    const id = location.hash.slice(1);
    // Wait a tick for sections to mount, then smooth-scroll.
    const t = window.setTimeout(() => {
      const el = document.getElementById(id);
      if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 60);
    return () => window.clearTimeout(t);
  }, [location.hash, location.key]);
  return (
    <PageShell>
      <Navbar />
      <HeroSection />
      <MetricsSection />
      <HowItWorks />
      <FeaturesSection />
      <UseCases />
      <PricingSection />
      <SocialProof />
      <FAQSection />

      {/* CTA Section */}
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
                No credit card required
              </div>
              <h2 id="cta-heading" className="text-3xl md:text-4xl font-heading font-bold text-foreground mb-4">
                Ready to create your perfect headshot?
              </h2>
              <p className="text-muted-foreground text-lg mb-8 max-w-md mx-auto">
                Create polished, professional photos for career, academic, business, and personal branding needs.
              </p>
              <Link
                to="/login"
                className="inline-flex items-center gap-2 px-8 py-4 rounded-xl gradient-cta text-primary-foreground font-semibold text-lg hover-scale btn-glow shadow-lg"
              >
                Create My Headshot
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

export default Index;
