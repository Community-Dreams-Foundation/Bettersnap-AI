import { motion } from "framer-motion";
import { Upload, Target, Palette, Sparkles, Download, ArrowRight } from "lucide-react";
import { Link } from "react-router-dom";

const steps = [
  {
    icon: Upload,
    title: "Upload Your Photos",
    description: "Upload clear, well-lit photos where your face is visible.",
  },
  {
    icon: Target,
    title: "Choose Your Purpose",
    description:
      "Select whether you need a photo for career, academic, business, personal branding, social, or documentation guidance.",
  },
  {
    icon: Palette,
    title: "Select Your Style",
    description: "Choose your preferred outfit, background, framing, and visual direction.",
  },
  {
    icon: Sparkles,
    title: "Generate Your Headshots",
    description:
      "BetterSnap AI creates multiple professional photo options based on your selections.",
  },
  {
    icon: Download,
    title: "Review and Download",
    description:
      "Choose your favorite result, adjust the format if needed, and download your image.",
  },
];

const HowItWorks = () => {
  return (
    <section id="how-it-works" className="py-24 relative" aria-labelledby="how-it-works-heading">
      <div className="container mx-auto px-4">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          className="text-center mb-16 max-w-3xl mx-auto"
        >
          <h2
            id="how-it-works-heading"
            className="text-3xl md:text-4xl lg:text-5xl font-heading font-bold text-foreground mb-5 leading-tight"
          >
            How <span className="text-gradient">BetterSnap AI</span> Works
          </h2>
          <p className="text-muted-foreground text-lg leading-relaxed">
            Create polished, ready-to-use headshots through a simple guided process.
          </p>
        </motion.div>

        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-6">
          {steps.map((step, i) => {
            const Icon = step.icon;
            return (
              <motion.div
                key={step.title}
                initial={{ opacity: 0, y: 20 }}
                whileInView={{ opacity: 1, y: 0 }}
                viewport={{ once: true }}
                transition={{ delay: i * 0.08 }}
                className="relative flex flex-col rounded-2xl border border-border bg-white/80 p-6 shadow-sm hover:shadow-lg hover:border-primary/20 transition-all duration-300"
              >
                <div className="flex items-center justify-between mb-4">
                  <span className="inline-flex items-center justify-center w-9 h-9 rounded-full bg-primary text-white font-heading font-bold text-sm">
                    {i + 1}
                  </span>
                  <Icon className="w-5 h-5 text-primary" aria-hidden="true" />
                </div>
                <h3 className="font-heading font-semibold text-base text-foreground mb-2 leading-snug">
                  {step.title}
                </h3>
                <p className="text-sm text-muted-foreground leading-relaxed">
                  {step.description}
                </p>
              </motion.div>
            );
          })}
        </div>

        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          className="mt-14 flex justify-center"
        >
          <Link
            to="/onboarding"
            className="inline-flex items-center gap-2 px-8 py-4 rounded-xl gradient-cta text-white font-semibold text-base hover-scale shadow-lg transition-all"
          >
            Create My Headshot
            <ArrowRight className="w-5 h-5" aria-hidden="true" />
          </Link>
        </motion.div>
      </div>
    </section>
  );
};

export default HowItWorks;
