import { useCallback, useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import useEmblaCarousel from "embla-carousel-react";
import { Star, Quote, ChevronLeft, ChevronRight } from "lucide-react";

/**
 * DEMO TESTIMONIALS — MVP PREVIEW ONLY
 * ------------------------------------
 * These records are fictional and intended for stakeholder review.
 * They are NOT verified customer reviews. To replace or remove later,
 * swap this array with approved testimonials (or filter `isDemo`).
 */
type DemoTestimonial = {
  name: string;
  role: string;
  text: string;
  rating: number;
  isDemo: true;
};

const demoTestimonials: DemoTestimonial[] = [
  { name: "Priya K.", role: "Graduate Student", text: "Uploading my photo was simple and the style options were clear. The result looked clean and professional.", rating: 5, isDemo: true },
  { name: "Carlos M.", role: "Job Seeker", text: "The guided flow made picking a LinkedIn-ready look really easy. I felt confident updating my profile.", rating: 5, isDemo: true },
  { name: "Mei L.", role: "International Student", text: "Helpful for getting a clean ID-style photo without booking a studio session. The instructions were easy to follow.", rating: 5, isDemo: true },
  { name: "Jordan T.", role: "Recent Graduate", text: "Loved being able to try multiple styles from one upload. Great option for refreshing my resume photo.", rating: 5, isDemo: true },
  { name: "Aisha R.", role: "Software Professional", text: "The resizing options for different platforms saved me time. Output looked polished and natural.", rating: 5, isDemo: true },
  { name: "Diego S.", role: "Freelance Designer", text: "Useful for personal branding across channels. The presets matched what I actually needed.", rating: 5, isDemo: true },
  { name: "Hannah W.", role: "Small-Business Owner", text: "A convenient alternative to scheduling a photographer for our team page updates.", rating: 5, isDemo: true },
  { name: "Liam O.", role: "Career Changer", text: "Made it easy to present a fresh, professional image while updating my profile for a new field.", rating: 5, isDemo: true },
  { name: "Sofia G.", role: "Content Creator", text: "Quick way to get a consistent look across social profiles. The style choices felt intentional.", rating: 5, isDemo: true },
  { name: "Noah P.", role: "Undergraduate Student", text: "The platform guidance was clear and beginner-friendly. I knew exactly which size to pick.", rating: 5, isDemo: true },
  { name: "Emma B.", role: "Job Seeker", text: "Clean upload experience and the previews helped me choose a style I actually liked.", rating: 5, isDemo: true },
  { name: "Yuki H.", role: "International Student", text: "Straightforward instructions in plain language made the whole process stress-free.", rating: 5, isDemo: true },
  { name: "Marcus D.", role: "Marketing Professional", text: "The personal-branding options are a thoughtful touch. Output felt on-brand for my role.", rating: 5, isDemo: true },
  { name: "Olivia F.", role: "Recent Graduate", text: "Helpful for getting a resume image when I didn't have time for a formal photo session.", rating: 5, isDemo: true },
  { name: "Ravi N.", role: "Freelancer", text: "I could pick different styles from one workflow, which is great for testing what works best.", rating: 5, isDemo: true },
  { name: "Chloe V.", role: "Small-Business Owner", text: "Simple, clear, and the cropping presets removed a lot of guesswork for me.", rating: 5, isDemo: true },
  { name: "Ethan J.", role: "Graduate Student", text: "Nice middle-ground between a phone selfie and a studio shoot. Felt polished without being stiff.", rating: 5, isDemo: true },
  { name: "Zara A.", role: "Career Changer", text: "The style picker helped me imagine how I'd appear in a new industry before committing.", rating: 5, isDemo: true },
  { name: "Lucas K.", role: "Software Professional", text: "Quick upload, clear preview, and easy export at the size I needed.", rating: 5, isDemo: true },
  { name: "Isabel C.", role: "Freelance Designer", text: "Loved the consistency across the different style outputs. Made my portfolio feel cohesive.", rating: 5, isDemo: true },
  { name: "Tom R.", role: "Job Seeker", text: "The platform guidance for each use case is genuinely useful for non-designers.", rating: 5, isDemo: true },
  { name: "Nina E.", role: "Content Creator", text: "Got several usable looks from one session. A practical tool for refreshing my channels.", rating: 5, isDemo: true },
  { name: "Ahmed Y.", role: "International Student", text: "Friendly experience and the result felt appropriate for academic profile use.", rating: 5, isDemo: true },
  { name: "Grace M.", role: "Undergraduate Student", text: "Great for students who want a clean, professional look without extra cost or scheduling.", rating: 5, isDemo: true },
  { name: "Ben L.", role: "Recent Graduate", text: "Easy to navigate and the export options matched the platforms I needed photos for.", rating: 5, isDemo: true },
];

const IDLE_RESUME_MS = 4000;

const SocialProof = () => {
  const prefersReducedMotion =
    typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

  const [emblaRef, emblaApi] = useEmblaCarousel({
    loop: !prefersReducedMotion,
    align: "start",
    dragFree: false,
  });

  const [paused, setPaused] = useState(false);
  const idleTimer = useRef<number | null>(null);
  const rafId = useRef<number | null>(null);
  const lastTick = useRef<number>(0);

  const clearIdleTimer = () => {
    if (idleTimer.current) {
      window.clearTimeout(idleTimer.current);
      idleTimer.current = null;
    }
  };

  const pause = useCallback(() => {
    setPaused(true);
    clearIdleTimer();
  }, []);

  const scheduleResume = useCallback(() => {
    clearIdleTimer();
    idleTimer.current = window.setTimeout(() => setPaused(false), IDLE_RESUME_MS);
  }, []);

  // Auto-scroll loop using requestAnimationFrame for smooth right-to-left motion.
  useEffect(() => {
    if (!emblaApi || prefersReducedMotion) return;

    const SPEED_PX_PER_MS = 0.03; // slow, smooth

    const tick = (ts: number) => {
      if (!emblaApi) return;
      if (!lastTick.current) lastTick.current = ts;
      const delta = ts - lastTick.current;
      lastTick.current = ts;

      if (!paused && !document.hidden) {
        const engine = emblaApi.internalEngine();
        const distance = SPEED_PX_PER_MS * delta;
        engine.location.add(-distance);
        engine.target.set(engine.location.get());
        engine.scrollLooper.loop(-1);
        engine.slideLooper.loop();
        engine.translate.to(engine.location.get());
      }
      rafId.current = requestAnimationFrame(tick);
    };

    rafId.current = requestAnimationFrame(tick);
    return () => {
      if (rafId.current) cancelAnimationFrame(rafId.current);
      lastTick.current = 0;
    };
  }, [emblaApi, paused, prefersReducedMotion]);

  // Pause when tab inactive
  useEffect(() => {
    const onVisibility = () => {
      if (document.hidden) pause();
      else scheduleResume();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, [pause, scheduleResume]);

  // Pause while user is dragging
  useEffect(() => {
    if (!emblaApi) return;
    const onPointerDown = () => pause();
    const onPointerUp = () => scheduleResume();
    emblaApi.on("pointerDown", onPointerDown);
    emblaApi.on("pointerUp", onPointerUp);
    return () => {
      emblaApi.off("pointerDown", onPointerDown);
      emblaApi.off("pointerUp", onPointerUp);
    };
  }, [emblaApi, pause, scheduleResume]);

  const scrollPrev = useCallback(() => {
    emblaApi?.scrollPrev();
    pause();
    scheduleResume();
  }, [emblaApi, pause, scheduleResume]);

  const scrollNext = useCallback(() => {
    emblaApi?.scrollNext();
    pause();
    scheduleResume();
  }, [emblaApi, pause, scheduleResume]);

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      scrollPrev();
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      scrollNext();
    }
  };

  return (
    <section className="py-24 relative overflow-hidden" aria-labelledby="testimonials-heading">
      <div className="container mx-auto px-4">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          className="text-center mb-12"
        >
          <div className="inline-flex items-center gap-2 px-3 py-1 rounded-full bg-primary/10 border border-primary/20 text-xs font-medium text-primary mb-4 uppercase tracking-wide">
            What People Are Saying
          </div>
          <h2
            id="testimonials-heading"
            className="text-3xl md:text-4xl font-heading font-bold text-foreground mb-4"
          >
            Trusted by <span className="text-gradient">students, professionals, and creators</span>
          </h2>
          <p className="text-muted-foreground max-w-2xl mx-auto">
            BetterSnap AI helps users create polished photos for career, academic, business, personal branding, and social use.
          </p>
        </motion.div>

        <div
          className="relative max-w-6xl mx-auto"
          onMouseEnter={pause}
          onMouseLeave={scheduleResume}
          onFocusCapture={pause}
          onBlurCapture={scheduleResume}
          onClick={() => {
            pause();
            scheduleResume();
          }}
          onTouchStart={pause}
          onTouchEnd={scheduleResume}
          onKeyDown={onKeyDown}
          tabIndex={0}
          role="region"
          aria-roledescription="carousel"
          aria-label="Demo testimonials"
        >
          <div className="overflow-hidden" ref={emblaRef}>
            <div className="flex">
              {demoTestimonials.map((t, i) => (
                <div
                  key={`${t.name}-${i}`}
                  className="flex-[0_0_100%] md:flex-[0_0_50%] lg:flex-[0_0_33.3333%] pl-4 first:pl-0 md:first:pl-4"
                >
                  <article className="h-full bg-card border border-border rounded-2xl p-6 shadow-sm hover:shadow-md transition-shadow flex flex-col min-h-[220px]">
                    <Quote className="w-7 h-7 text-primary/40 mb-3" aria-hidden="true" />
                    <div
                      className="flex gap-0.5 mb-3"
                      aria-label={`${t.rating} out of 5 stars (demo)`}
                    >
                      {Array.from({ length: t.rating }).map((_, j) => (
                        <Star key={j} className="w-4 h-4 fill-primary text-primary" aria-hidden="true" />
                      ))}
                    </div>
                    <p className="text-foreground text-sm leading-relaxed mb-4 flex-1">"{t.text}"</p>
                    <div>
                      <p className="font-semibold text-foreground text-sm">{t.name}</p>
                      <p className="text-xs text-muted-foreground">{t.role}</p>
                    </div>
                  </article>
                </div>
              ))}
            </div>
          </div>

          <div className="flex items-center justify-center gap-3 mt-8">
            <button
              type="button"
              onClick={scrollPrev}
              aria-label="Previous testimonial"
              className="w-11 h-11 rounded-full bg-card border border-border shadow-sm hover:bg-primary hover:text-primary-foreground hover:border-primary transition-colors flex items-center justify-center focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <ChevronLeft className="w-5 h-5" aria-hidden="true" />
            </button>
            <button
              type="button"
              onClick={scrollNext}
              aria-label="Next testimonial"
              className="w-11 h-11 rounded-full bg-card border border-border shadow-sm hover:bg-primary hover:text-primary-foreground hover:border-primary transition-colors flex items-center justify-center focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <ChevronRight className="w-5 h-5" aria-hidden="true" />
            </button>
          </div>
        </div>
      </div>
    </section>
  );
};

export default SocialProof;
