import { motion } from "framer-motion";

const metrics = [
  { value: "12k+", label: "Students Served" },
  { value: "30s", label: "Generation Time" },
  { value: "8+", label: "Headshot Styles" },
  { value: "99%", label: "Satisfaction Rate" },
];

const MetricsSection = () => {
  return (
    <section className="relative" aria-label="Company metrics">
      <div className="container mx-auto px-4">
        <div className="border-t border-border" aria-hidden="true" />
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.5 }}
          className="py-16"
        >
          <div className="grid grid-cols-2 md:grid-cols-4 gap-8 md:gap-0">
            {metrics.map((metric, i) => (
              <div
                key={metric.label}
                className={`flex flex-col items-center text-center ${
                  i < metrics.length - 1
                    ? "md:border-r md:border-border"
                    : ""
                }`}
              >
                <span className="text-4xl md:text-5xl font-heading font-bold text-foreground mb-2">
                  {metric.value}
                </span>
                <span className="text-sm md:text-base text-muted-foreground font-medium">
                  {metric.label}
                </span>
              </div>
            ))}
          </div>
        </motion.div>
      </div>
    </section>
  );
};

export default MetricsSection;
