// Hair color examples (gender-aware)
import mBlack from "@/assets/hair/male-black.jpg";
import mDarkBrown from "@/assets/hair/male-dark-brown.jpg";
import mBrown from "@/assets/hair/male-brown.jpg";
import mLightBrown from "@/assets/hair/male-light-brown.jpg";
import mBlonde from "@/assets/hair/male-blonde.jpg";
import mAuburn from "@/assets/hair/male-auburn.jpg";
import mRed from "@/assets/hair/male-red.jpg";
import mGray from "@/assets/hair/male-gray.jpg";
import mWhite from "@/assets/hair/male-white.jpg";
import mBald from "@/assets/hair/male-bald.jpg";

import fBlack from "@/assets/hair/female-black.jpg";
import fDarkBrown from "@/assets/hair/female-dark-brown.jpg";
import fBrown from "@/assets/hair/female-brown.jpg";
import fLightBrown from "@/assets/hair/female-light-brown.jpg";
import fBlonde from "@/assets/hair/female-blonde.jpg";
import fAuburn from "@/assets/hair/female-auburn.jpg";
import fRed from "@/assets/hair/female-red.jpg";
import fGray from "@/assets/hair/female-gray.jpg";
import fWhite from "@/assets/hair/female-white.jpg";
import fBald from "@/assets/hair/female-bald.jpg";

import customHair from "@/assets/hair/custom-other.jpg";

// Background examples
import bgWhite from "@/assets/backgrounds/clean-white.jpg";
import bgSoftGray from "@/assets/backgrounds/soft-light-gray.jpg";
import bgNeutralGray from "@/assets/backgrounds/neutral-gray.jpg";
import bgCharcoal from "@/assets/backgrounds/dark-charcoal.jpg";
import bgBlackStudio from "@/assets/backgrounds/black-studio.jpg";
import bgModernOffice from "@/assets/backgrounds/modern-office.jpg";
import bgExecOffice from "@/assets/backgrounds/executive-office.jpg";
import bgCorpLobby from "@/assets/backgrounds/corporate-lobby.jpg";
import bgCoworking from "@/assets/backgrounds/coworking-space.jpg";
import bgLibrary from "@/assets/backgrounds/library-academic.jpg";
import bgCampus from "@/assets/backgrounds/university-campus.jpg";
import bgOutdoor from "@/assets/backgrounds/outdoor-professional.jpg";
import bgCity from "@/assets/backgrounds/city-background.jpg";
import bgWarm from "@/assets/backgrounds/warm-studio.jpg";
import bgGradient from "@/assets/backgrounds/soft-gradient.jpg";
import bgBrand from "@/assets/backgrounds/brand-color.jpg";
import bgCustom from "@/assets/backgrounds/custom-background.jpg";

// Use case images (reuse existing use-case portraits)
import ucNetworking from "@/assets/usecases/professional-networking-1.jpg";
import ucResume from "@/assets/usecases/resume-job-applications-1.jpg";
import ucStudent from "@/assets/usecases/student-academic-1.jpg";
import ucTeam from "@/assets/usecases/business-team-profiles-1.jpg";
import ucBranding from "@/assets/usecases/personal-branding-1.jpg";
import ucSocial from "@/assets/usecases/social-lifestyle-1.jpg";
import ucTravel from "@/assets/usecases/travel-documentation-1.jpg";
import ucCustom from "@/assets/usecases/custom-purpose.jpg";

export type GenderSet = "male" | "female" | "neutral";

export interface HairColorOption {
  id: string;
  label: string;
  images: { male: string; female: string; neutral: string };
}

// "neutral" alternates male/female portraits for diverse, inclusive examples
export const hairColors: HairColorOption[] = [
  { id: "black",        label: "Black",          images: { male: mBlack,      female: fBlack,      neutral: fBlack } },
  { id: "dark_brown",   label: "Dark Brown",     images: { male: mDarkBrown,  female: fDarkBrown,  neutral: mDarkBrown } },
  { id: "brown",        label: "Brown",          images: { male: mBrown,      female: fBrown,      neutral: fBrown } },
  { id: "light_brown",  label: "Light Brown",    images: { male: mLightBrown, female: fLightBrown, neutral: mLightBrown } },
  { id: "blonde",       label: "Blonde",         images: { male: mBlonde,     female: fBlonde,     neutral: fBlonde } },
  { id: "auburn",       label: "Auburn",         images: { male: mAuburn,     female: fAuburn,     neutral: mAuburn } },
  { id: "red",          label: "Red",            images: { male: mRed,        female: fRed,        neutral: fRed } },
  { id: "gray",         label: "Gray",           images: { male: mGray,       female: fGray,       neutral: mGray } },
  { id: "white",        label: "White",          images: { male: mWhite,      female: fWhite,      neutral: fWhite } },
  { id: "bald",         label: "Bald or Shaved", images: { male: mBald,       female: fBald,       neutral: mBald } },
  { id: "custom",       label: "Other / Custom", images: { male: customHair,  female: customHair,  neutral: customHair } },
];

export const genderToSet = (g: string): GenderSet =>
  g === "male" ? "male" : g === "female" ? "female" : "neutral";

// Use-case options
export interface UseCaseOption {
  id: string;
  label: string;
  description: string;
  examples: string[];
  image: string;
  recommendedBgs: string[];
  note?: string;
}

export const useCaseOptions: UseCaseOption[] = [
  {
    id: "networking",
    label: "Professional Networking",
    description: "For LinkedIn, business profiles, company directories, and professional networking.",
    examples: ["LinkedIn", "Business profile", "Corporate website", "Company directory", "Speaker profile"],
    image: ucNetworking,
    recommendedBgs: ["modern_office", "clean_white", "soft_light_gray"],
  },
  {
    id: "resume",
    label: "Resume & Job Applications",
    description: "For resumes, CVs, job applications, and career opportunities.",
    examples: ["Resume", "CV", "Recruitment profile", "Career portfolio", "Job application"],
    image: ucResume,
    recommendedBgs: ["clean_white", "soft_light_gray", "neutral_gray"],
  },
  {
    id: "student",
    label: "Student & Academic",
    description: "For university profiles, student directories, scholarships, and academic use.",
    examples: ["University profile", "Student directory", "Scholarship", "Academic portfolio", "Graduation profile"],
    image: ucStudent,
    recommendedBgs: ["library_academic", "university_campus", "clean_white"],
  },
  {
    id: "team",
    label: "Business & Team Profiles",
    description: "For employee directories, leadership pages, team profiles, and company websites.",
    examples: ["Employee directory", "Leadership page", "Sales team", "Company profile", "Team website"],
    image: ucTeam,
    recommendedBgs: ["corporate_lobby", "modern_office", "brand_color"],
  },
  {
    id: "branding",
    label: "Personal Branding",
    description: "For personal websites, portfolios, creators, media kits, and professional branding.",
    examples: ["Portfolio", "Creator profile", "Personal website", "Media kit", "Social profile"],
    image: ucBranding,
    recommendedBgs: ["warm_studio", "city_background", "outdoor_professional"],
  },
  {
    id: "social",
    label: "Social & Lifestyle",
    description: "For social profiles, dating, lifestyle, travel, and personal use.",
    examples: ["Social media", "Dating profile", "Travel profile", "Lifestyle content", "Personal use"],
    image: ucSocial,
    recommendedBgs: ["outdoor_professional", "city_background", "soft_gradient"],
  },
  {
    id: "travel",
    label: "Travel & Documentation",
    description: "For supported document-photo formatting and travel-related guidance.",
    examples: ["Passport guidance", "Visa guidance", "Travel form", "Document formatting", "ID-style photo guidance"],
    image: ucTravel,
    recommendedBgs: ["clean_white", "soft_light_gray"],
    note: "BetterSnap AI provides formatting and photo guidance only. Final acceptance is determined by the relevant issuing authority.",
  },
  {
    id: "custom",
    label: "Custom Purpose",
    description: "Choose this option if your photo need does not match the available categories.",
    examples: [],
    image: ucCustom,
    recommendedBgs: [],
  },
];

// Backgrounds
export interface BackgroundOption {
  id: string;
  label: string;
  description: string;
  image: string;
}

export const backgroundOptions: BackgroundOption[] = [
  { id: "clean_white",          label: "Clean White",          description: "Best for resumes and clean professional profiles.", image: bgWhite },
  { id: "soft_light_gray",      label: "Soft Light Gray",      description: "Versatile and suitable for most professional uses.", image: bgSoftGray },
  { id: "neutral_gray",         label: "Neutral Gray",         description: "Classic neutral tone that works across industries.", image: bgNeutralGray },
  { id: "dark_charcoal",        label: "Dark Charcoal",        description: "Refined, modern look for formal portraits.", image: bgCharcoal },
  { id: "black_studio",         label: "Black Studio",         description: "Bold, dramatic studio portrait style.", image: bgBlackStudio },
  { id: "modern_office",        label: "Modern Office",        description: "Recommended for LinkedIn and business profiles.", image: bgModernOffice },
  { id: "executive_office",     label: "Executive Office",     description: "Suitable for leadership, consulting, and corporate profiles.", image: bgExecOffice },
  { id: "corporate_lobby",      label: "Corporate Lobby",      description: "Polished corporate environment for team and leadership pages.", image: bgCorpLobby },
  { id: "coworking_space",      label: "Co-working Space",     description: "Modern, creative setting for startups and creators.", image: bgCoworking },
  { id: "library_academic",     label: "Library or Academic",  description: "Recommended for student, academic, and university profiles.", image: bgLibrary },
  { id: "university_campus",    label: "University Campus",    description: "Suitable for graduation, scholarship, and student profiles.", image: bgCampus },
  { id: "outdoor_professional", label: "Outdoor Professional", description: "A natural professional option for personal branding.", image: bgOutdoor },
  { id: "city_background",      label: "City Background",      description: "Urban, lifestyle feel for creators and personal brands.", image: bgCity },
  { id: "warm_studio",          label: "Warm Studio",          description: "A polished and approachable portrait style.", image: bgWarm },
  { id: "soft_gradient",        label: "Soft Gradient",        description: "Subtle modern gradient for social and lifestyle photos.", image: bgGradient },
  { id: "brand_color",          label: "Brand Color",          description: "Use a color that matches your personal or company brand.", image: bgBrand },
  { id: "custom_background",    label: "Custom Background",    description: "Choose or upload a supported custom background.", image: bgCustom },
];
