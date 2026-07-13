import { motion } from "framer-motion";
import { ArrowRight } from "lucide-react";
import { Link } from "react-router-dom";
import { useCaseCategories } from "@/data/useCases";

const UseCases = () => {
  return (
    <section id="use-cases" className="py-24 relative" aria-labelledby="use-cases-heading">
      <div className="container mx-auto px-4">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          className="text-center mb-14"
        >
          <h2
            id="use-cases-heading"
            className="text-3xl md:text-4xl font-heading font-bold text-foreground mb-4"
          >
            Explore BetterSnap AI <span className="text-gradient">Use Cases</span>
          </h2>
          <p className="text-muted-foreground text-lg max-w-2xl mx-auto">
            From professional networking to personal branding — find the right format for every moment.
          </p>
        </motion.div>

        <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-5 max-w-6xl mx-auto">
          {useCaseCategories.map((uc, i) => {
            const Icon = uc.icon;
            return (
              <motion.div
                key={uc.slug}
                initial={{ opacity: 0, y: 20 }}
                whileInView={{ opacity: 1, y: 0 }}
                viewport={{ once: true }}
                transition={{ delay: i * 0.05 }}
              >
                <Link
                  to={`/use-cases/${uc.slug}`}
                  className="group glass-card rounded-2xl p-6 hover-scale block h-full"
                >
                  <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-primary/20 to-accent/10 flex items-center justify-center mb-4 group-hover:scale-110 transition-transform">
                    <Icon className="w-6 h-6 text-foreground" aria-hidden="true" />
                  </div>
                  <h3 className="font-heading font-semibold text-lg text-foreground mb-1.5">
                    {uc.title}
                  </h3>
                  <p className="text-sm text-muted-foreground leading-relaxed mb-4">
                    {uc.shortDesc}
                  </p>
                  <span className="inline-flex items-center gap-1.5 text-sm font-medium text-primary group-hover:text-primary/80">
                    Learn more <ArrowRight className="w-3.5 h-3.5" />
                  </span>
                </Link>
              </motion.div>
            );
          })}
        </div>

        <div className="text-center mt-10">
          <Link
            to={`/use-cases/${useCaseCategories[0].slug}`}
            className="inline-flex items-center gap-2 px-6 py-3 rounded-xl bg-white border border-border text-foreground font-semibold hover:bg-muted/50 transition-colors"
          >
            View All Use Cases <ArrowRight className="w-4 h-4" />
          </Link>
        </div>
      </div>
    </section>
  );
};

export default UseCases;
