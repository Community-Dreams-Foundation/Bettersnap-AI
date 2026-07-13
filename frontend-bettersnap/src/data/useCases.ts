import {
  Briefcase,
  FileText,
  GraduationCap,
  Users,
  Sparkles,
  Plane,
  Heart,
  type LucideIcon,
} from "lucide-react";

// Professional Networking
import pn1 from "@/assets/usecases/professional-networking-1.jpg";
import pn2 from "@/assets/usecases/professional-networking-2.jpg";
import pn3 from "@/assets/usecases/professional-networking-3.jpg";
import pn4 from "@/assets/usecases/professional-networking-4.jpg";
// Resume & Job Applications
import rj1 from "@/assets/usecases/resume-job-applications-1.jpg";
import rj2 from "@/assets/usecases/resume-job-applications-2.jpg";
import rj3 from "@/assets/usecases/resume-job-applications-3.jpg";
import rj4 from "@/assets/usecases/resume-job-applications-4.jpg";
// Student & Academic
import sa1 from "@/assets/usecases/student-academic-1.jpg";
import sa2 from "@/assets/usecases/student-academic-2.jpg";
import sa3 from "@/assets/usecases/student-academic-3.jpg";
import sa4 from "@/assets/usecases/student-academic-4.jpg";
// Business & Team Profiles
import bt1 from "@/assets/usecases/business-team-profiles-1.jpg";
import bt2 from "@/assets/usecases/business-team-profiles-2.jpg";
import bt3 from "@/assets/usecases/business-team-profiles-3.jpg";
import bt4 from "@/assets/usecases/business-team-profiles-4.jpg";
// Personal Branding
import pb1 from "@/assets/usecases/personal-branding-1.jpg";
import pb2 from "@/assets/usecases/personal-branding-2.jpg";
import pb3 from "@/assets/usecases/personal-branding-3.jpg";
import pb4 from "@/assets/usecases/personal-branding-4.jpg";
// Travel & Documentation
import td1 from "@/assets/usecases/travel-documentation-1.jpg";
import td2 from "@/assets/usecases/travel-documentation-2.jpg";
import td3 from "@/assets/usecases/travel-documentation-3.jpg";
import td4 from "@/assets/usecases/travel-documentation-4.jpg";
// Social & Lifestyle
import sl1 from "@/assets/usecases/social-lifestyle-1.jpg";
import sl2 from "@/assets/usecases/social-lifestyle-2.jpg";
import sl3 from "@/assets/usecases/social-lifestyle-3.jpg";
import sl4 from "@/assets/usecases/social-lifestyle-4.jpg";

export interface UseCaseExampleImage {
  label: string;
  src: string;
}

export interface UseCaseCategory {
  slug: string;
  title: string;
  shortDesc: string;
  icon: LucideIcon;
  heroHeading: string;
  supportingText: string;
  examples: string[];
  exampleImages: UseCaseExampleImage[];
  benefits: { title: string; desc: string }[];
  recommendedStyles: string[];
  guidance: string[];
  disclaimer?: string;
}

export const useCaseCategories: UseCaseCategory[] = [
  {
    slug: "professional-networking",
    title: "Professional Networking",
    shortDesc: "LinkedIn, business profiles, corporate websites, speaker pages.",
    icon: Briefcase,
    heroHeading: "Make a confident first impression everywhere you connect",
    supportingText:
      "Create polished headshots for LinkedIn, business profiles, company directories, corporate websites, and speaker profiles.",
    examples: ["LinkedIn profile", "Business profile", "Corporate website", "Company directory", "Speaker profile"],
    exampleImages: [
      { label: "Corporate headshot", src: pn1 },
      { label: "Business-casual", src: pn2 },
      { label: "Executive profile", src: pn3 },
      { label: "Speaker profile", src: pn4 },
    ],
    benefits: [
      { title: "Professional presentation", desc: "Polished, recruiter-ready look." },
      { title: "Recommended backgrounds", desc: "Neutral and corporate-friendly." },
      { title: "Platform-ready framing", desc: "LinkedIn and business profile sizes." },
      { title: "Multiple style options", desc: "Pick the tone that fits your role." },
    ],
    recommendedStyles: ["Corporate", "Business Casual", "Clean Background", "Creative Professional"],
    guidance: [
      "Use a clear front-facing photo",
      "Choose good, even lighting",
      "Avoid blurry or heavily filtered images",
      "Keep only one person in the photo",
      "Use a neutral, confident expression",
    ],
  },
  {
    slug: "resume-job-applications",
    title: "Resume & Job Applications",
    shortDesc: "Resumes, CVs, recruitment profiles, career portfolios.",
    icon: FileText,
    heroHeading: "Put your best professional image forward",
    supportingText:
      "Create clean, professional photos for resumes, CVs, job applications, recruitment profiles, and career portfolios.",
    examples: ["Resume", "CV", "Job application", "Recruitment profile", "Career portfolio"],
    exampleImages: [
      { label: "Formal resume headshot", src: rj1 },
      { label: "Job-application profile", src: rj2 },
      { label: "Business-casual portrait", src: rj3 },
      { label: "Career portfolio photo", src: rj4 },
    ],
    benefits: [
      { title: "Professional presentation", desc: "Hiring-manager friendly look." },
      { title: "Recommended backgrounds", desc: "Clean, distraction-free." },
      { title: "Resize and format guidance", desc: "Right size for any application." },
      { title: "Multiple style options", desc: "Match your target industry." },
    ],
    recommendedStyles: ["Corporate", "Business Casual", "Clean Background", "Student-Friendly"],
    guidance: [
      "Use a clear front-facing photo",
      "Choose good lighting",
      "Avoid blurry or heavily filtered images",
      "Keep only one person in the photo",
      "Use a neutral facial expression",
    ],
  },
  {
    slug: "student-academic",
    title: "Student & Academic",
    shortDesc: "University profiles, student directories, scholarships, portfolios.",
    icon: GraduationCap,
    heroHeading: "Create a polished profile for every academic opportunity",
    supportingText:
      "Generate professional photos for university profiles, student directories, scholarship applications, academic portfolios, and graduation profiles.",
    examples: ["University profile", "Student directory", "Scholarship application", "Academic portfolio", "Graduation profile"],
    exampleImages: [
      { label: "University profile", src: sa1 },
      { label: "Student directory", src: sa2 },
      { label: "Scholarship profile", src: sa3 },
      { label: "Graduation portrait", src: sa4 },
    ],
    benefits: [
      { title: "Student-friendly styles", desc: "Approachable and professional." },
      { title: "Recommended backgrounds", desc: "Clean, academic-friendly." },
      { title: "Platform-ready framing", desc: "University ID and directory sizing." },
      { title: "Multiple style options", desc: "From scholarship to portfolio." },
    ],
    recommendedStyles: ["Student-Friendly", "Clean Background", "Business Casual", "Corporate"],
    guidance: [
      "Use a clear front-facing photo",
      "Choose good, natural lighting",
      "Avoid blurry or heavily filtered images",
      "Keep only one person in the photo",
      "Use a neutral, friendly expression",
    ],
  },
  {
    slug: "business-team-profiles",
    title: "Business & Team Profiles",
    shortDesc: "Employee directories, leadership pages, team headshots.",
    icon: Users,
    heroHeading: "Build a consistent professional image across your organization",
    supportingText:
      "Create polished headshots for employee directories, leadership pages, sales teams, company profiles, and team websites.",
    examples: ["Employee directory", "Leadership page", "Sales team", "Company profile", "Team headshots"],
    exampleImages: [
      { label: "Employee directory", src: bt1 },
      { label: "Leadership profile", src: bt2 },
      { label: "Sales professional", src: bt3 },
      { label: "Team website portrait", src: bt4 },
    ],
    benefits: [
      { title: "Consistent team look", desc: "Cohesive styling across people." },
      { title: "Recommended backgrounds", desc: "Brand-friendly neutrals." },
      { title: "Platform-ready framing", desc: "Directory and website sizes." },
      { title: "Multiple style options", desc: "Leadership to entry-level." },
    ],
    recommendedStyles: ["Corporate", "Business Casual", "Clean Background", "Creative Professional"],
    guidance: [
      "Use clear, well-lit photos for the whole team",
      "Pick a consistent background across members",
      "Avoid heavy filters",
      "One person per photo",
      "Keep expressions natural and professional",
    ],
  },
  {
    slug: "personal-branding",
    title: "Personal Branding",
    shortDesc: "Personal sites, portfolios, creator profiles, media kits.",
    icon: Sparkles,
    heroHeading: "Create a consistent image for your personal brand",
    supportingText:
      "Generate professional photos for personal websites, portfolios, creator profiles, media kits, and social platforms.",
    examples: ["Personal website", "Creator profile", "Portfolio", "Social profile", "Media kit"],
    exampleImages: [
      { label: "Creator profile", src: pb1 },
      { label: "Personal website", src: pb2 },
      { label: "Portfolio headshot", src: pb3 },
      { label: "Modern branded profile", src: pb4 },
    ],
    benefits: [
      { title: "On-brand styling", desc: "A look that reflects you." },
      { title: "Recommended backgrounds", desc: "Editorial and clean." },
      { title: "Multi-platform framing", desc: "Web, social, and press kits." },
      { title: "Multiple style options", desc: "From editorial to lifestyle." },
    ],
    recommendedStyles: ["Personal Branding", "Creative Professional", "Lifestyle", "Clean Background"],
    guidance: [
      "Use a clear front-facing photo",
      "Choose lighting that reflects your brand tone",
      "Avoid heavy filters",
      "Keep only one person in the photo",
      "Use an expression that fits your audience",
    ],
  },
  {
    slug: "travel-documentation",
    title: "Travel & Documentation",
    shortDesc: "Passport, visa, and travel form photo guidance.",
    icon: Plane,
    heroHeading: "Prepare clear, well-formatted photos for travel-related needs",
    supportingText:
      "Get photo formatting and usage guidance for passports, visas, travel forms, and other supported document-photo requirements.",
    examples: ["Passport photo guidance", "Visa photo guidance", "Travel forms", "Document photo formatting"],
    exampleImages: [
      { label: "Neutral background", src: td1 },
      { label: "Clear front-facing", src: td2 },
      { label: "Travel-form example", src: td3 },
      { label: "Document-format example", src: td4 },
    ],
    benefits: [
      { title: "Format guidance", desc: "Sizing and framing helpers." },
      { title: "Recommended backgrounds", desc: "Plain, neutral tones." },
      { title: "Clear framing", desc: "Centered, front-facing layout." },
      { title: "Resize guidance", desc: "Common document presets." },
    ],
    recommendedStyles: ["Clean Background", "Corporate", "Business Casual"],
    guidance: [
      "Use a clear front-facing photo",
      "Use even, neutral lighting",
      "Avoid filters and accessories that obscure the face",
      "Keep only one person in the photo",
      "Use a neutral expression",
    ],
    disclaimer:
      "BetterSnap AI provides photo formatting and usage guidance. Final acceptance for passports, visas, IDs, or official documents is determined by the relevant issuing authority.",
  },
  {
    slug: "social-lifestyle",
    title: "Social & Lifestyle",
    shortDesc: "Dating, travel, lifestyle, and social media photos.",
    icon: Heart,
    heroHeading: "Create polished photos for the moments that matter",
    supportingText:
      "Generate natural, high-quality images for dating profiles, travel, lifestyle content, social media, and personal use.",
    examples: ["Dating profile", "Travel profile", "Lifestyle image", "Social media profile", "Personal use"],
    exampleImages: [
      { label: "Dating profile", src: sl1 },
      { label: "Travel lifestyle", src: sl2 },
      { label: "Social media profile", src: sl3 },
      { label: "Natural personal photo", src: sl4 },
    ],
    benefits: [
      { title: "Natural look", desc: "Approachable and authentic." },
      { title: "Recommended backgrounds", desc: "Lifestyle-friendly settings." },
      { title: "Platform-ready framing", desc: "Sized for popular social apps." },
      { title: "Multiple style options", desc: "From casual to refined." },
    ],
    recommendedStyles: ["Lifestyle", "Outdoor Professional", "Personal Branding", "Business Casual"],
    guidance: [
      "Use a clear front-facing photo",
      "Choose flattering, natural lighting",
      "Avoid heavy filters",
      "Keep only one person in the photo",
      "Use a relaxed, genuine expression",
    ],
  },
];

export const getUseCaseBySlug = (slug?: string) =>
  useCaseCategories.find((c) => c.slug === slug);
