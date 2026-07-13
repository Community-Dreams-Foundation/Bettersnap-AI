import { motion } from "framer-motion";
import { Link } from "react-router-dom";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";

const faqs = [
  {
    q: "How does BetterSnap AI work?",
    a: "Upload a clear photo, choose your use case and preferred style, generate your headshots, and download the results you like.",
  },
  {
    q: "What kind of photo should I upload?",
    a: "Use a clear, front-facing photo with good lighting. Avoid blurry images, heavy filters, sunglasses, extreme angles, and photos containing multiple people.",
  },
  {
    q: "What types of photos can I create?",
    a: "BetterSnap AI supports professional networking, job applications, academic profiles, business pages, personal branding, social profiles, and supported travel or document-photo guidance.",
  },
  {
    q: "Can I choose my outfit, background, or style?",
    a: "Yes. Users can select from available professional and personal styles, including different backgrounds, outfits, and presentation options.",
  },
  {
    q: "How long does generation take?",
    a: "Generation time may vary depending on image quality, selected style, system demand, and processing requirements.",
  },
  {
    q: "Can I resize my headshot?",
    a: "Yes. BetterSnap AI can provide supported resize options and formatting guidance for professional profiles, resumes, university profiles, websites, and social platforms.",
  },
  {
    q: "Can I use BetterSnap AI for passport or visa photos?",
    a: "BetterSnap AI may provide photo formatting and usage guidance. Final acceptance is determined by the relevant government or issuing authority.",
  },
  {
    q: "How are my uploaded photos handled?",
    a: "Uploaded photos are processed according to the BetterSnap AI Privacy Policy and approved data-retention practices.",
  },
  {
    q: "Can I delete my uploaded or generated photos?",
    a: "Users should be able to delete uploaded and generated images from their account, based on the approved retention process.",
  },
  {
    q: "How do credits and payments work?",
    a: "BetterSnap AI may offer monthly plans, one-time headshot packages, and flexible credit-based options. Details should be shown in the Pricing section.",
  },
];

const FAQSection = () => {
  return (
    <section id="faq" className="py-24 relative" aria-labelledby="faq-heading">
      <div className="container mx-auto px-4 max-w-3xl">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          className="text-center mb-12"
        >
          <h2
            id="faq-heading"
            className="text-3xl md:text-4xl lg:text-5xl font-heading font-bold text-foreground mb-5 leading-tight"
          >
            Frequently Asked <span className="text-gradient">Questions</span>
          </h2>
          <p className="text-muted-foreground text-lg leading-relaxed">
            Find quick answers about photo uploads, AI generation, pricing, downloads, privacy, and account usage.
          </p>
        </motion.div>

        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          className="rounded-2xl border border-border bg-white/80 shadow-sm px-6 md:px-8"
        >
          <Accordion type="single" collapsible className="w-full">
            {faqs.map((faq, i) => (
              <AccordionItem
                key={i}
                value={`item-${i}`}
                className={i === faqs.length - 1 ? "border-b-0" : ""}
              >
                <AccordionTrigger className="text-left font-heading font-semibold text-foreground hover:no-underline py-5">
                  {faq.q}
                </AccordionTrigger>
                <AccordionContent className="text-muted-foreground leading-relaxed text-base">
                  {faq.a}
                </AccordionContent>
              </AccordionItem>
            ))}
          </Accordion>
        </motion.div>

        <p className="text-center mt-10 text-muted-foreground">
          Still need help?{" "}
          <Link
            to="/contact-support"
            className="text-primary font-medium hover:underline underline-offset-4"
          >
            Contact Support
          </Link>
        </p>
      </div>
    </section>
  );
};

export default FAQSection;
