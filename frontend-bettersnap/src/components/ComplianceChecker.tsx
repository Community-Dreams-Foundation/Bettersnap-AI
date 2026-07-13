import { Check, X, Shield } from "lucide-react";
import { motion } from "framer-motion";

interface ComplianceItem {
  label: string;
  passed: boolean;
}

interface ComplianceCheckerProps {
  useCase: string;
  items?: ComplianceItem[];
}

const defaultChecks: Record<string, ComplianceItem[]> = {
  LinkedIn: [
    { label: "Size: 400x400px minimum", passed: true },
    { label: "Resolution quality: Max 8MB, JPG/PNG/GIF", passed: true },
    { label: "Face fills 60% of frame", passed: true },
    { label: "Professional background", passed: true },
    { label: "Good lighting", passed: true },
  ],
  Resume: [
    { label: "Size: Universal standard (600×600px)", passed: true },
    { label: "Background: Professional preferred", passed: true },
    { label: "Professional appearance", passed: true },
    { label: "Proper cropping", passed: true },
    { label: "Resolution/Quality: Print-quality 300 DPI", passed: true },
  ],
  "University ID": [
    { label: "Solid background", passed: true },
    { label: "Face centered", passed: true },
    { label: "Correct aspect ratio", passed: true },
    { label: "No accessories covering face", passed: true },
    { label: "Resolution quality: Varies", passed: true },
  ],
};

const ComplianceChecker = ({ useCase, items }: ComplianceCheckerProps) => {
  const checks = items || defaultChecks[useCase] || defaultChecks["LinkedIn"];
  const allPassed = checks.every((c) => c.passed);
  const passedCount = checks.filter((c) => c.passed).length;

  return (
    <div className="glass rounded-2xl p-6 shadow-glass" role="region" aria-label="Compliance check results">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <Shield className="w-5 h-5 text-primary" aria-hidden="true" />
          <h3 className="font-heading font-semibold text-foreground">Fit check</h3>
        </div>
        <span
          className={`text-xs font-semibold px-3 py-1 rounded-full ${
            allPassed ? "bg-success/15 text-success" : "bg-destructive/15 text-destructive"
          }`}
          role="status"
        >
          {allPassed ? "All Passed" : `${passedCount}/${checks.length} Passed`}
        </span>
      </div>
      <p className="text-sm text-muted-foreground mb-4">{useCase} requirements</p>
      <ul className="space-y-3" aria-label="Compliance requirements">
        {checks.map((check, i) => (
          <motion.li
            key={check.label}
            initial={{ opacity: 0, x: -10 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ delay: i * 0.08 }}
            className="flex items-center gap-3"
          >
            <div
              className={`w-6 h-6 rounded-full flex items-center justify-center flex-shrink-0 ${
                check.passed ? "bg-success/15" : "bg-destructive/15"
              }`}
              aria-hidden="true"
            >
              {check.passed ? (
                <Check className="w-3.5 h-3.5 text-success" />
              ) : (
                <X className="w-3.5 h-3.5 text-destructive" />
              )}
            </div>
            <span className="text-sm text-foreground">{check.label}</span>
          </motion.li>
        ))}
      </ul>
    </div>
  );
};

export default ComplianceChecker;
