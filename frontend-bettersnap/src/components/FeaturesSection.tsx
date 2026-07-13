import { motion } from "framer-motion";
import {
  Camera,
  Layers,
  Palette,
  Maximize2,
  BookOpen,
  UserCheck,
  Zap,
  ArrowRight,
} from "lucide-react";
import { Link } from "react-router-dom";

const features = [
  {
    icon: Camera,
    title: "Professional AI Headshot Studio",
    description:
      "Create polished AI-generated headshots from your uploaded photos for different personal and professional needs.",
    highlight: true,
  },
  {
    icon: Layers,
    title: "Multi-Purpose Photo Categories",
    description:
      "Choose from guided categories for career, academic, business, personal branding, social, and lifestyle use.",
    highlight: true,
  },
  {
    icon: Palette,
    title: "Smart Style Selection",
    description:
      "Select outfit, background, framing, and presentation styles that match your goal.",
    highlight: false,
  },
  {
    icon: Maximize2,
    title: "Platform-Ready Resize Tools",
    description:
      "Prepare your image for different digital profiles, applications, websites, and supported formats.",
    highlight: false,
  },
  {
    icon: BookOpen,
    title: "Usage & Format Guidance",
    description:
      "Get helpful guidance on where each photo style works best and how to prepare your image for use.",
    highlight: false,
  },
  {
    icon: UserCheck,
    title: "Personal Branding Ready",
    description:
      "Create a consistent, professional look across your online presence, portfolio, business pages, and social platforms.",
    highlight: true,
  },
  {
    icon: Zap,
    title: "Simple Guided Workflow",
    description:
      "Follow a clear step-by-step process from upload to style selection, generation, review, and download.",
    highlight: false,
  },
];

const FeaturesSection = () => {
  return (
    <section id="features" className="py-24 relative" aria-labelledby="features-heading">
      <div className="container mx-auto px-4">
        {/* Header */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          className="text-center mb-16 max-w-4xl mx-auto"
        >
          <h2
            id="features-heading"
            className="text-3xl md:text-4xl lg:text-5xl font-heading font-bold text-foreground mb-5 leading-tight"
          >
            Everything you need to create the{" "}
            <span className="text-gradient">right photo</span> for every moment
          </h2>
          <p className="text-muted-foreground text-lg leading-relaxed">
            BetterSnap AI helps you generate polished, ready-to-use photos for career growth,
            academic profiles, business presence, personal branding, and social and lifestyle use.
          </p>
        </motion.div>

        {/* Feature Grid */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-6 lg:gap-8">
          {features.map((feature, i) => {
            const Icon = feature.icon;
            return (
              <motion.div
                key={feature.title}
                initial={{ opacity: 0, y: 20 }}
                whileInView={{ opacity: 1, y: 0 }}
                viewport={{ once: true }}
                transition={{ delay: i * 0.05 }}
                className={`group relative flex flex-col rounded-2xl border p-7 transition-all duration-300 hover:-translate-y-1 ${
                  feature.highlight
                    ? "border-primary/30 bg-white shadow-lg shadow-primary/10 hover:shadow-xl hover:shadow-primary/15"
                    : "border-border bg-white/80 shadow-sm hover:shadow-lg hover:border-primary/20"
                }`}
              >
                {/* Icon */}
                <div
                  className={`mb-5 inline-flex items-center justify-center w-11 h-11 rounded-xl ${
                    feature.highlight
                      ? "bg-primary/10 text-primary"
                      : "bg-muted text-foreground/70 group-hover:bg-primary/10 group-hover:text-primary"
                  } transition-colors duration-300`}
                >
                  <Icon className="w-5 h-5" aria-hidden="true" />
                </div>

                {/* Title */}
                <h3 className="font-heading font-semibold text-lg text-foreground mb-3 leading-snug">
                  {feature.title}
                </h3>

                {/* Description */}
                <p className="text-sm text-muted-foreground leading-relaxed flex-1">
                  {feature.description}
                </p>
              </motion.div>
            );
          })}
        </div>

        {/* CTAs */}
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          className="mt-16 flex flex-col sm:flex-row items-center justify-center gap-4"
        >
          <Link
            to="/onboarding"
            className="inline-flex items-center gap-2 px-8 py-4 rounded-xl gradient-cta text-white font-semibold text-base hover-scale shadow-lg transition-all"
          >
            Try BetterSnap AI
            <ArrowRight className="w-5 h-5" aria-hidden="true" />
          </Link>
          <a
            href="#pricing"
            className="text-sm font-medium text-muted-foreground hover:text-foreground transition-colors underline-offset-4 hover:underline"
          >
            View Pricing
          </a>
        </motion.div>
      </div>
    </section>
  );
};

export default FeaturesSection;
