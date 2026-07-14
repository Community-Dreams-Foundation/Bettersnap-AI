import { useState, useCallback } from "react";
import { motion } from "framer-motion";
import { ArrowRight, Sparkles, Palette, Download } from "lucide-react";
import { Link } from "react-router-dom";
import headshotBefore from "@/assets/headshot-before.jpg";
import headshotAfter from "@/assets/headshot-after.jpg";

const HeroSection = () => {
  const [sliderPos, setSliderPos] = useState(50);
  const [isDragging, setIsDragging] = useState(false);

  const handleMove = useCallback(
    (clientX: number, rect: DOMRect) => {
      if (!isDragging) return;
      const x = Math.max(0, Math.min(clientX - rect.left, rect.width));
      setSliderPos((x / rect.width) * 100);
    },
    [isDragging],
  );

  const onPointerDown = () => setIsDragging(true);
  const onPointerUp = () => setIsDragging(false);

  return (
    <section className="relative flex pt-12 pb-16 overflow-hidden" role="banner">
      <div className="container relative z-10 mx-auto px-4 py-4 lg:py-8">
        <div className="grid lg:grid-cols-2 gap-12 lg:gap-16 items-center">
          {/* Left column */}
          <motion.div initial={{ opacity: 0, y: 30 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.6 }}>



            <h1 className="text-4xl md:text-5xl lg:text-6xl font-heading font-bold text-foreground leading-[1.05] mb-6 max-w-2xl">
              AI-Powered Photos for <span className="text-primary">Every Version of You</span>
            </h1>

            <p className="text-lg text-muted-foreground mb-6 max-w-lg leading-relaxed">
              Upload your photos and create polished, studio-quality images for career growth, academic profiles, business presence, personal branding, dating profiles, social media, and everyday life.
            </p>
            {/* Benefit tags */}
            <div className="flex flex-wrap gap-3 mb-8">
              {[
                { icon: Sparkles, label: "Career & Business" },
                { icon: Palette, label: "Personal & Social" },
                { icon: Download, label: "Branding & Lifestyle" },
              ].map(({ icon: Icon, label }) => (
                <div
                  key={label}
                  className="inline-flex items-center gap-2 px-4 py-2 rounded-full bg-white border border-slate-200 text-slate-700 text-sm font-medium shadow-sm"
                >
                  <Icon className="w-4 h-4 text-primary" />
                  {label}
                </div>
              ))}
            </div>

            {/* CTA buttons */}
            <div className="flex flex-col sm:flex-row gap-4 mb-8 max-w-lg">
              <Link
                to="/onboarding"
                className="inline-flex items-center justify-center gap-2 px-8 h-14 rounded-full gradient-cta text-primary-foreground font-semibold shadow-lg hover-scale whitespace-nowrap"
              >
                Create My Headshot
                <ArrowRight className="w-4 h-4" />
              </Link>
              <button
                type="button"
                onClick={() => document.getElementById("how-it-works")?.scrollIntoView({ behavior: "smooth" })}
                className="inline-flex items-center justify-center gap-2 px-8 h-14 rounded-full bg-white border border-slate-200 text-slate-700 font-semibold shadow-sm hover:bg-slate-50 hover:shadow-md transition-all whitespace-nowrap"
              >
                See How It Works
              </button>
            </div>

            {/* Social proof avatars */}
            <div className="flex items-center gap-3 mt-10">
              <div className="flex -space-x-2.5" aria-hidden="true">
                {[
                  "https://randomuser.me/api/portraits/women/44.jpg",
                  "https://randomuser.me/api/portraits/men/32.jpg",
                  "https://randomuser.me/api/portraits/women/68.jpg",
                  "https://randomuser.me/api/portraits/men/75.jpg",
                ].map((src, i) => (
                  <img
                    key={i}
                    src={src}
                    alt=""
                    loading="lazy"
                    className="w-9 h-9 rounded-full border-2 border-border object-cover"
                  />
                ))}
              </div>
              <p className="text-sm text-muted-foreground">
                Trusted by professionals, students, and creators
              </p>
            </div>
          </motion.div>

          {/* Right column — Before/After comparison */}
          <motion.div
            initial={{ opacity: 0, scale: 0.95 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{ duration: 0.6, delay: 0.2 }}
            className="relative"
          >
            <div className="glass rounded-3xl p-3 shadow-glass">
              {/* Labels */}
              <div className="flex justify-between px-4 mb-3">
                <span
                  className="
                    px-3
                    py-1
                    rounded-full
                    bg-white
                    text-slate-600
                    text-xs
                    font-semibold
                    shadow-sm
                    uppercase
                    tracking-wide
                  "
                >
                  Before
                </span>

                <span
                  className="
                    px-3
                    py-1
                    rounded-full
                    bg-primary
                    text-white
                    text-xs
                    font-semibold
                    shadow-sm
                    uppercase
                    tracking-wide
                  "
                >
                  After
                </span>
              </div>

              {/* Comparison slider */}
              <div
                className="relative w-full aspect-[4/3] rounded-2xl overflow-hidden cursor-col-resize select-none"
                onPointerDown={onPointerDown}
                onPointerUp={onPointerUp}
                onPointerLeave={onPointerUp}
                onPointerMove={(e) => handleMove(e.clientX, e.currentTarget.getBoundingClientRect())}
                onTouchMove={(e) => {
                  const touch = e.touches[0];
                  const rect = e.currentTarget.getBoundingClientRect();
                  const x = Math.max(0, Math.min(touch.clientX - rect.left, rect.width));
                  setSliderPos((x / rect.width) * 100);
                }}
                role="slider"
                aria-label="Before and after comparison slider"
                aria-valuenow={Math.round(sliderPos)}
                aria-valuemin={0}
                aria-valuemax={100}
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === "ArrowLeft") setSliderPos((p) => Math.max(0, p - 2));
                  if (e.key === "ArrowRight") setSliderPos((p) => Math.min(100, p + 2));
                }}
              >
                {/* After image (full) */}
                <img
                  src={headshotAfter}
                  alt="Professional headshot after AI enhancement"
                  className="absolute inset-0 w-full h-full object-cover"
                />
                {/* Before image (clipped) */}
                <div className="absolute inset-0 overflow-hidden" style={{ width: `${sliderPos}%` }}>
                  <img
                    src={headshotBefore}
                    alt="Casual selfie before AI enhancement"
                    className="absolute inset-0 w-full h-full object-cover"
                    style={{ minWidth: "100%", width: `${10000 / sliderPos}%`, maxWidth: "none" }}
                  />
                </div>
                {/* Slider line + handle */}
                <div
                  className="absolute top-0 bottom-0 w-0.5 bg-white/80 z-10"
                  style={{ left: `${sliderPos}%`, transform: "translateX(-50%)" }}
                >
                  <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-8 h-8 rounded-full bg-white/90 shadow-lg flex items-center justify-center backdrop-blur-sm border border-white/40">
                    <div className="w-3 h-3 rounded-full gradient-cta" />
                  </div>
                </div>
              </div>

              {/* Slider progress bar */}
              <div className="mx-4 mt-3 mb-1">
                <div className="h-1 rounded-full bg-white/10 overflow-hidden">
                  <div
                    className="h-full gradient-cta rounded-full transition-all duration-75"
                    style={{ width: `${sliderPos}%` }}
                  />
                </div>
              </div>
            </div>
          </motion.div>
        </div>
      </div>
    </section>
  );
};

export default HeroSection;
