import { motion } from "framer-motion";
import { Link } from "react-router-dom";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import {
  Sparkles,
  ImageIcon,
  Briefcase,
  Users,
  ShieldCheck,
  CreditCard,
  type LucideIcon,
} from "lucide-react";

interface FAQItem {
  q: string;
  a: string;
}

interface FAQCategory {
  id: string;
  title: string;
  description: string;
  icon: LucideIcon;
  items: FAQItem[];
}

const categories: FAQCategory[] = [
  {
    id: "general",
    title: "General & Product Overview",
    description: "What BetterSnap AI is and how the experience works.",
    icon: Sparkles,
    items: [
      {
        q: "What exactly is BetterSnap AI?",
        a: "BetterSnap AI is a guided AI headshot platform that helps users create high-quality digital portraits without booking a traditional studio photoshoot. Users upload clear photos, train a personal AI model, choose a professional style, and generate polished headshots for professional profiles, job applications, student portfolios, personal branding, and team directories.",
      },
      {
        q: "How does BetterSnap AI create a professional headshot?",
        a: "BetterSnap AI uses your uploaded photos to understand your facial features and create new professional-looking portraits with improved lighting, backgrounds, clothing styles, and composition while keeping your likeness recognizable.",
      },
      {
        q: "What makes BetterSnap AI different from other AI photo tools?",
        a: "BetterSnap AI focuses on guided headshot generation instead of generic photo creation. The experience is designed around real-world needs such as professional profiles, resumes, student use, company directories, and personal branding.",
      },
      {
        q: "How long does the process take?",
        a: "Training your personal AI model can take around 30–35 minutes depending on server availability and GPU processing. Once your model is ready, you can generate headshots using the available styles and backgrounds.",
      },
      {
        q: "Will the generated headshots look like me?",
        a: "BetterSnap AI is designed to preserve your likeness, but final quality depends heavily on the photos you upload. Clear, natural, well-lit images with different angles help produce the best results.",
      },
    ],
  },
  {
    id: "uploads",
    title: "Uploads & Image Quality",
    description: "How to get the best results from your uploaded photos.",
    icon: ImageIcon,
    items: [
      {
        q: "How many photos should I upload?",
        a: "BetterSnap AI works best when you upload 8–12 clear photos. This gives the system enough variety to understand your face accurately.",
      },
      {
        q: "What types of photos work best?",
        a: "Use sharp, well-lit photos where your face is clearly visible. Upload a mix of front-facing and slightly angled photos with natural expressions and different backgrounds or lighting conditions.",
      },
      {
        q: "What photos should I avoid?",
        a: "Avoid blurry photos, heavy filters, sunglasses, group photos, extreme shadows, covered faces, cropped faces, or images where your face is too small or unclear.",
      },
      {
        q: "Can I use selfies or phone pictures?",
        a: "Yes. Smartphone photos work well as long as they are clear, high-resolution, and taken in good lighting.",
      },
      {
        q: "What if my final headshots do not look right?",
        a: "AI results depend on the quality of the uploaded photos and selected styles. If the result does not look accurate, try uploading a fresh set of clearer photos with better lighting, natural expressions, and fewer obstructions.",
      },
    ],
  },
  {
    id: "use-cases",
    title: "Use Cases & Personal Branding",
    description: "Where and how to use your BetterSnap AI headshots.",
    icon: Briefcase,
    items: [
      {
        q: "Can I use BetterSnap AI headshots for LinkedIn, resumes, and websites?",
        a: "Yes. BetterSnap AI is designed for professional profiles, resumes, portfolios, company websites, online directories, and personal branding.",
      },
      {
        q: "Can I use the photos for dating profiles or casual social media?",
        a: "Yes. While BetterSnap AI focuses on professional headshots, the clean lighting and polished results can also be useful for personal branding, social media, and dating profiles.",
      },
      {
        q: "Can students and job seekers generate multiple styles?",
        a: "Yes. After training your personal model, you can generate multiple headshot styles depending on available plans, credits, and style options.",
      },
      {
        q: "Can I choose backgrounds and outfit styles?",
        a: "Yes. BetterSnap AI provides guided style options such as professional attire and background selections. Available options may depend on your plan.",
      },
      {
        q: "Can I use BetterSnap AI images for passports, visas, or official IDs?",
        a: "BetterSnap AI may provide clean backgrounds or general formatting guidance, but we do not guarantee approval for passports, visas, immigration documents, government IDs, or any official documents. Government agencies often require strict biometric standards, and AI-generated or edited images may not be accepted. Always check the official requirements before submitting any image.",
      },
    ],
  },
  {
    id: "teams",
    title: "Teams & Organizations",
    description: "Options for companies, universities, and remote teams.",
    icon: Users,
    items: [
      {
        q: "Can companies use BetterSnap AI for team headshots?",
        a: "Yes. BetterSnap AI can support individuals and teams who need consistent, professional-looking headshots for company profiles, staff directories, websites, and internal systems.",
      },
      {
        q: "Can our team get a unified visual style?",
        a: "Yes. Team-style generation can help create a consistent look using similar backgrounds, lighting, and professional styling.",
      },
      {
        q: "Can remote employees use BetterSnap AI?",
        a: "Yes. Remote users can upload photos from anywhere and generate headshots without attending an in-person photoshoot.",
      },
      {
        q: "Are team dashboards or bulk management features available?",
        a: "Team and organization features may vary by package or product rollout. Contact the BetterSnap AI team for current availability and setup options.",
      },
    ],
  },
  {
    id: "privacy",
    title: "Privacy, Security & Data Handling",
    description: "How your photos and account information are protected.",
    icon: ShieldCheck,
    items: [
      {
        q: "Are my uploaded photos private?",
        a: "Yes. Uploaded photos are used to provide your selected BetterSnap AI service and are handled according to the platform’s Privacy Policy.",
      },
      {
        q: "Do you use my photos to train general AI models?",
        a: "No. Your uploaded photos are used for your own headshot generation experience. BetterSnap AI does not use your personal photos to train general AI models unless explicitly stated in the Privacy Policy or separately permitted by you.",
      },
      {
        q: "Can I delete my photos?",
        a: "BetterSnap AI aims to give users control over their data. Data deletion and retention are handled according to the active Privacy Policy and available account settings.",
      },
      {
        q: "How long are uploaded photos stored?",
        a: "Storage and retention periods follow BetterSnap AI’s active Privacy Policy. Please review the Privacy Policy for the most current details.",
      },
      {
        q: "Are payment details secure?",
        a: "BetterSnap AI uses secure third-party payment processors when payments are available. Sensitive payment details are handled by the payment provider and are not stored directly in plain form by BetterSnap AI.",
      },
    ],
  },
  {
    id: "pricing",
    title: "Pricing, Credits & Support",
    description: "How credits, plans, refunds, and support work.",
    icon: CreditCard,
    items: [
      {
        q: "How much does BetterSnap AI cost?",
        a: "BetterSnap AI offers monthly plans and one-time image packs. Monthly plans are designed for recurring headshot generation, while one-time image packs are useful if you need a fixed number of headshots without a subscription. Final checkout will be available once Stripe integration is finalized.",
      },
      {
        q: "What are credits?",
        a: "Credits are used to generate headshots or access available style options. Credit usage may vary depending on the selected plan and number of images generated.",
      },
      {
        q: "Do credits expire?",
        a: "Credit expiration depends on the plan or package selected. Please review the pricing details shown during purchase or inside your account.",
      },
      {
        q: "What happens if generation fails?",
        a: "If a technical issue prevents successful generation, BetterSnap AI may restore credits or provide regeneration support according to the active support and refund policy.",
      },
      {
        q: "Can I get a refund if I am unhappy with the result?",
        a: "Refund eligibility depends on BetterSnap AI’s active refund policy. Because AI images are digital outputs generated on demand, refund and correction options may vary by case.",
      },
      {
        q: "How can I get help?",
        a: "Users can contact BetterSnap AI support for help with uploads, training status, credits, generated results, account questions, or team setup.",
      },
    ],
  },
];

const FAQSection = () => {
  return (
    <section id="faq" className="py-24 relative" aria-labelledby="faq-heading">
      <div className="container mx-auto px-4 max-w-4xl">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          className="text-center mb-14"
        >
          <div className="inline-flex items-center gap-2 px-4 py-1.5 rounded-full bg-primary/10 text-primary text-xs font-medium mb-4">
            <Sparkles className="w-3.5 h-3.5" aria-hidden="true" />
            Help Center
          </div>
          <h2
            id="faq-heading"
            className="text-3xl md:text-4xl lg:text-5xl font-heading font-bold text-foreground mb-5 leading-tight"
          >
            BetterSnap AI <span className="text-gradient">FAQ</span>
          </h2>
          <p className="text-muted-foreground text-lg leading-relaxed max-w-2xl mx-auto">
            Everything you need to know about uploading photos, training your personal AI model,
            choosing styles, and generating professional headshots.
          </p>
        </motion.div>

        <div className="space-y-8">
          {categories.map((cat, ci) => {
            const Icon = cat.icon;
            return (
              <motion.div
                key={cat.id}
                initial={{ opacity: 0, y: 20 }}
                whileInView={{ opacity: 1, y: 0 }}
                viewport={{ once: true }}
                transition={{ delay: ci * 0.05 }}
                className="rounded-2xl border border-border bg-white/80 shadow-sm overflow-hidden"
              >
                <div className="flex items-start gap-4 px-6 md:px-8 py-6 border-b border-border bg-gradient-to-r from-primary/5 via-accent/5 to-transparent">
                  <div
                    className="w-11 h-11 rounded-xl bg-gradient-to-br from-primary/20 to-accent/15 flex items-center justify-center flex-shrink-0"
                    aria-hidden="true"
                  >
                    <Icon className="w-5 h-5 text-primary" />
                  </div>
                  <div>
                    <h3 className="font-heading font-semibold text-xl text-foreground leading-tight">
                      {cat.title}
                    </h3>
                    <p className="text-sm text-muted-foreground mt-1">{cat.description}</p>
                  </div>
                </div>

                <div className="px-6 md:px-8">
                  <Accordion type="single" collapsible className="w-full">
                    {cat.items.map((faq, i) => (
                      <AccordionItem
                        key={i}
                        value={`${cat.id}-${i}`}
                        className={i === cat.items.length - 1 ? "border-b-0" : ""}
                      >
                        <AccordionTrigger className="text-left font-heading font-semibold text-foreground hover:no-underline py-5">
                          {faq.q}
                        </AccordionTrigger>
                        <AccordionContent className="text-muted-foreground leading-relaxed text-base pb-5">
                          {faq.a}
                        </AccordionContent>
                      </AccordionItem>
                    ))}
                  </Accordion>
                </div>
              </motion.div>
            );
          })}
        </div>

        <p className="text-center mt-12 text-muted-foreground">
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
