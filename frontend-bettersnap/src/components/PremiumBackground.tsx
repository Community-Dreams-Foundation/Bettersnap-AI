import { motion } from "framer-motion";
import { useState } from "react";

const PARTICLE_COUNT = 24;

interface Particle {
  id: number;
  x: number;
  y: number;
  size: number;
  opacity: number;
  duration: number;
  delay: number;
}

const generateParticles = (): Particle[] =>
  Array.from({ length: PARTICLE_COUNT }, (_, i) => ({
    id: i,
    x: Math.random() * 100,
    y: Math.random() * 100,
    size: Math.random() * 2 + 1,
    opacity: Math.random() * 0.18 + 0.05,
    duration: Math.random() * 20 + 15,
    delay: Math.random() * -20,
  }));

const PremiumBackground = () => {
  const [particles] = useState<Particle[]>(generateParticles);

  return (
    <div className="fixed inset-0 z-0 overflow-hidden pointer-events-none" aria-hidden="true">
      {/* Soft lavender-white base */}
      <div className="absolute inset-0" style={{ background: "#F0F0FF" }} />

      {/* Soft indigo glow — top-left */}
      <div
        className="absolute w-[900px] h-[900px] -top-48 -left-48 rounded-full opacity-40 blur-[150px]"
        style={{ background: "radial-gradient(circle, hsl(245 79% 61% / 0.35) 0%, transparent 65%)" }}
      />

      {/* Soft purple glow — center */}
      <div
        className="absolute w-[700px] h-[700px] top-1/4 left-1/3 rounded-full opacity-30 blur-[130px]"
        style={{ background: "radial-gradient(circle, hsl(260 80% 70% / 0.3) 0%, transparent 65%)" }}
      />

      {/* Soft indigo glow — right */}
      <div
        className="absolute w-[800px] h-[800px] top-1/3 -right-32 rounded-full opacity-35 blur-[140px]"
        style={{ background: "radial-gradient(circle, hsl(245 79% 61% / 0.3) 0%, transparent 65%)" }}
      />

      {/* Animated pulsing overlay */}
      <motion.div
        className="absolute w-[500px] h-[500px] top-1/4 left-1/2 rounded-full blur-[160px]"
        style={{ background: "radial-gradient(circle, hsl(245 79% 61% / 0.2) 0%, transparent 70%)" }}
        animate={{ opacity: [0.15, 0.3, 0.15], scale: [1, 1.1, 1] }}
        transition={{ duration: 8, repeat: Infinity, ease: "easeInOut" }}
      />

      {/* Subtle grid overlay */}
      <div
        className="absolute inset-0 opacity-[0.04]"
        style={{
          backgroundImage: `
            linear-gradient(hsl(231 37% 17% / 0.4) 1px, transparent 1px),
            linear-gradient(90deg, hsl(231 37% 17% / 0.4) 1px, transparent 1px)
          `,
          backgroundSize: "60px 60px",
        }}
      />

      {/* Floating particles */}
      <svg className="absolute inset-0 w-full h-full" xmlns="http://www.w3.org/2000/svg">
        {particles.map((p) => (
          <motion.circle
            key={p.id}
            cx={`${p.x}%`}
            cy={`${p.y}%`}
            r={p.size}
            fill="hsl(245 79% 61%)"
            opacity={p.opacity}
            animate={{
              cy: [`${p.y}%`, `${p.y - 6 - Math.random() * 10}%`, `${p.y}%`],
              cx: [`${p.x}%`, `${p.x + (Math.random() - 0.5) * 5}%`, `${p.x}%`],
              opacity: [p.opacity, p.opacity * 2, p.opacity],
            }}
            transition={{
              duration: p.duration,
              repeat: Infinity,
              ease: "easeInOut",
              delay: p.delay,
            }}
          />
        ))}
      </svg>
    </div>
  );
};

export default PremiumBackground;
