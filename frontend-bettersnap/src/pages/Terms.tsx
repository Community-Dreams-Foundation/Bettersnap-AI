import { Link } from "react-router-dom";
import { useEffect, useState } from "react";
import { ArrowLeft, ArrowUp, AlertTriangle, Mail } from "lucide-react";
import PageShell from "@/components/PageShell";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

const sections = [
  { id: "acceptance", title: "1. Acceptance of Terms" },
  { id: "description", title: "2. Description of Service" },
  { id: "eligibility", title: "3. Eligibility — Age Requirement" },
  { id: "accounts", title: "4. User Accounts and Registration" },
  { id: "credits", title: "5. Credit System, Subscriptions & Purchases" },
  { id: "permitted-use", title: "6. Permitted Use and Intellectual Property" },
  { id: "id-documents", title: "7. Government & Official ID Disclaimer" },
  { id: "warranties", title: "8. User Representations and Warranties" },
  { id: "ai-limits", title: "9. AI Output Limitations & Beta Disclaimer" },
  { id: "disclaimer", title: "10. Disclaimer of Warranties" },
  { id: "liability", title: "11. Limitation of Liability" },
  { id: "indemnification", title: "12. Indemnification" },
  { id: "termination", title: "13. Termination" },
  { id: "governing-law", title: "14. Governing Law & Dispute Resolution" },
  { id: "changes", title: "15. Changes to These Terms" },
  { id: "contact", title: "16. Company Information & Contact" },
];

const styles = [
  "Business Suit",
  "Healthcare / Medical",
  "Realtor",
  "Lawyer / Legal",
  "Dark Studio",
  "Formal Non-Suit",
  "Dating Profile",
  "Travel Photo",
  "Graduation Photo",
  "Beach / Sunset",
  "Luxury / Lifestyle",
  "Custom Scene",
];

const plans: Array<{ plan: string; price: string; credits: string; notes: string }> = [
  { plan: "Free Tier", price: "No charge", credits: "20 one-time credits", notes: "Granted upon account creation" },
  { plan: "Basic Tier", price: "$20 / month", credits: "300 credits / month", notes: "Credits expire at end of billing cycle" },
  { plan: "Pro Tier", price: "$45 / month", credits: "750 credits / month", notes: "Credits expire at end of billing cycle" },
  { plan: "Pro+ Tier", price: "$80 / month", credits: "1,500 credits / month", notes: "Credits expire at end of billing cycle" },
  { plan: "Top-Up Package", price: "$5 each", credits: "50 credits per package", notes: "One-time, no expiry" },
];

const Terms = () => {
  const [activeId, setActiveId] = useState(sections[0].id);
  const [showTop, setShowTop] = useState(false);

  useEffect(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((e) => e.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible[0]) setActiveId(visible[0].target.id);
      },
      { rootMargin: "-20% 0px -70% 0px", threshold: 0 },
    );
    sections.forEach((s) => {
      const el = document.getElementById(s.id);
      if (el) observer.observe(el);
    });
    const onScroll = () => setShowTop(window.scrollY > 600);
    window.addEventListener("scroll", onScroll);
    return () => {
      observer.disconnect();
      window.removeEventListener("scroll", onScroll);
    };
  }, []);

  const jump = (id: string) => {
    const el = document.getElementById(id);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
      setActiveId(id);
    }
  };

  return (
    <PageShell>
      <Navbar />
      <main className="container mx-auto px-4 py-12 lg:py-16 max-w-7xl print:py-4">
        <Link
          to="/"
          className="inline-flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground mb-6 transition-colors print:hidden"
        >
          <ArrowLeft className="w-4 h-4" /> Back to home
        </Link>

        <header className="mb-8">
          <h1 className="text-4xl md:text-5xl font-heading font-bold text-foreground mb-3">Terms &amp; Conditions</h1>
          <p className="text-lg text-muted-foreground mb-2">
            Please review the terms that apply when using BetterSnap AI.
          </p>
          <p className="text-sm text-muted-foreground">Last updated: June 2026</p>
          <p className="mt-5 text-foreground/90 max-w-3xl leading-relaxed">
            These Terms &amp; Conditions govern your access to and use of BetterSnap AI, including the website, account
            features, AI-powered headshot and lifestyle photo generation services, credit-based plans, subscriptions,
            one-time purchases, and related services.
          </p>
        </header>

        {/* Mobile TOC */}
        <div className="lg:hidden mb-8 print:hidden">
          <label className="block text-xs uppercase tracking-wider text-muted-foreground mb-2">Jump to section</label>
          <Select value={activeId} onValueChange={jump}>
            <SelectTrigger className="w-full bg-card border-border text-foreground">
              <SelectValue placeholder="Select a section" />
            </SelectTrigger>
            <SelectContent>
              {sections.map((s) => (
                <SelectItem key={s.id} value={s.id}>
                  {s.title}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="lg:grid lg:grid-cols-[260px_1fr] lg:gap-10">
          <aside className="hidden lg:block print:hidden">
            <nav
              aria-label="Table of contents"
              className="sticky top-24 rounded-2xl p-5 bg-card border border-border max-h-[calc(100vh-7rem)] overflow-y-auto"
            >
              <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-4">Contents</p>
              <ul className="space-y-1">
                {sections.map((s) => {
                  const isActive = activeId === s.id;
                  return (
                    <li key={s.id}>
                      <button
                        onClick={() => jump(s.id)}
                        className={`w-full text-left text-sm px-3 py-2 rounded-lg transition-colors border-l-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary ${
                          isActive
                            ? "bg-primary/10 text-primary border-primary font-medium"
                            : "text-muted-foreground hover:text-foreground hover:bg-muted border-transparent"
                        }`}
                      >
                        {s.title}
                      </button>
                    </li>
                  );
                })}
              </ul>
            </nav>
          </aside>

          <article className="space-y-10 text-foreground" style={{ fontSize: "16px", lineHeight: 1.75 }}>
            <Section id="acceptance" title="1. Acceptance of Terms">
              <p>
                By accessing or using BetterSnap AI, you acknowledge that you have read, understood, and agreed to these
                Terms &amp; Conditions and the BetterSnap AI Privacy Policy. If you do not agree, please do not use the
                service.
              </p>
            </Section>

            <Section id="description" title="2. Description of Service">
              <p>
                BetterSnap AI transforms user-uploaded photos into professional headshots and lifestyle images using
                artificial intelligence.
              </p>
              <ul className="list-disc pl-6 space-y-1 mt-3">
                <li>Professional headshots</li>
                <li>Lifestyle photos</li>
                <li>Style selection</li>
                <li>Custom backgrounds</li>
                <li>Attire changes</li>
                <li>4K upscaling where available</li>
                <li>Credit-based system</li>
                <li>Subscription tiers</li>
                <li>One-time purchase options</li>
              </ul>
              <p className="mt-4 font-medium">Available style examples:</p>
              <div className="mt-2 flex flex-wrap gap-2">
                {styles.map((s) => (
                  <span key={s} className="px-3 py-1 rounded-full bg-primary/10 text-primary text-xs font-medium">
                    {s}
                  </span>
                ))}
              </div>
            </Section>

            <Section id="eligibility" title="3. Eligibility — Age Requirement">
              <ul className="list-disc pl-6 space-y-1">
                <li>BetterSnap AI is intended only for users who are 18 years of age or older.</li>
                <li>By using the service, you confirm you are at least 18.</li>
                <li>The service is not directed to individuals under 18.</li>
                <li>
                  If BetterSnap AI discovers an under-18 account, the account may be terminated and associated data
                  deleted.
                </li>
              </ul>
            </Section>

            <Section id="accounts" title="4. User Accounts and Registration">
              <p>Users are responsible for:</p>
              <ul className="list-disc pl-6 space-y-1 mt-2">
                <li>Maintaining account security.</li>
                <li>Providing accurate account information.</li>
                <li>Updating information when needed.</li>
                <li>Reporting unauthorized account activity.</li>
              </ul>
              <p className="mt-3">
                Support email:{" "}
                <a href="mailto:support@bettersnap.ai" className="text-primary hover:underline">
                  support@bettersnap.ai
                </a>
              </p>
            </Section>

            <Section id="credits" title="5. Credit System, Subscriptions & Purchases">
              <div className="overflow-x-auto rounded-xl border border-border">
                <table className="w-full text-sm">
                  <thead className="bg-muted/50">
                    <tr className="text-left">
                      <th className="px-4 py-3 font-semibold">Plan</th>
                      <th className="px-4 py-3 font-semibold">Price</th>
                      <th className="px-4 py-3 font-semibold">Credits</th>
                      <th className="px-4 py-3 font-semibold">Notes</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {plans.map((p) => (
                      <tr key={p.plan}>
                        <td className="px-4 py-3 font-medium">{p.plan}</td>
                        <td className="px-4 py-3">{p.price}</td>
                        <td className="px-4 py-3">{p.credits}</td>
                        <td className="px-4 py-3 text-muted-foreground">{p.notes}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <h3 className="font-heading font-semibold text-foreground mt-6 mb-2">Credit mechanics</h3>
              <ul className="list-disc pl-6 space-y-1">
                <li>Credits are deducted when the job is submitted.</li>
                <li>Failed or errored generations trigger an automatic credit refund within 60 seconds.</li>
                <li>Monthly subscription credits do not roll over.</li>
                <li>Top-up credits do not expire.</li>
                <li>Credits have no cash value and are non-transferable unless expressly permitted.</li>
              </ul>

              <h3 className="font-heading font-semibold text-foreground mt-6 mb-2">Subscription renewal and cancellation</h3>
              <ul className="list-disc pl-6 space-y-1">
                <li>Subscriptions renew automatically unless cancelled at least 24 hours before renewal.</li>
                <li>You may cancel through the account dashboard or by contacting support.</li>
                <li>Cancellation takes effect at the end of the current paid period.</li>
                <li>You retain access until that date.</li>
                <li>Price increases for existing subscribers require at least 30 days’ advance notice.</li>
              </ul>

              <h3 className="font-heading font-semibold text-foreground mt-6 mb-2">Refund and regeneration policy</h3>
              <ul className="list-disc pl-6 space-y-1">
                <li>Sales of credits and subscriptions are final except as required by law or as described in these Terms.</li>
                <li>Failed generations caused by platform errors refund credits automatically within 60 seconds.</li>
                <li>If an AI-generated image is materially defective, you may contact support within 7 days.</li>
                <li>BetterSnap AI may issue a credit refund or regeneration at its discretion.</li>
                <li>You may request one complimentary regeneration per order if the result is materially inconsistent with the selected style.</li>
                <li>Additional regenerations use credits.</li>
                <li>Unused one-time package credits may be refunded within 14 days if no credits from that package were used.</li>
              </ul>

              <h3 className="font-heading font-semibold text-foreground mt-6 mb-2">Payment processing</h3>
              <ul className="list-disc pl-6 space-y-1">
                <li>Payments are processed by Stripe.</li>
                <li>BetterSnap AI does not directly collect or store full payment card information.</li>
                <li>You agree to Stripe’s Terms when completing a purchase.</li>
              </ul>
            </Section>

            <Section id="permitted-use" title="6. Permitted Use and Intellectual Property">
              <ul className="list-disc pl-6 space-y-1">
                <li>You retain ownership of original uploaded photos.</li>
                <li>You also own AI-generated images created for you through the service.</li>
                <li>You grant BetterSnap AI a limited license to process uploaded images only as needed to provide the service.</li>
                <li>Generated images may be used for lawful personal, professional, and business-profile purposes.</li>
              </ul>

              <h3 className="font-heading font-semibold text-foreground mt-6 mb-2">Allowed uses</h3>
              <ul className="list-disc pl-6 space-y-1">
                <li>LinkedIn profiles</li>
                <li>Professional resumes</li>
                <li>Business websites</li>
                <li>Freelance portfolios</li>
                <li>Personal branding</li>
                <li>Commercial self-promotion</li>
                <li>Employee directories</li>
                <li>Corporate profiles</li>
                <li>Conference materials</li>
              </ul>

              <h3 className="font-heading font-semibold text-foreground mt-6 mb-2">Prohibited uses</h3>
              <ul className="list-disc pl-6 space-y-1">
                <li>Reselling or exploiting the platform, API, or model without permission</li>
                <li>Reverse engineering</li>
                <li>Scraping</li>
                <li>Uploading third-party photos without consent</li>
                <li>Creating misleading identities or deepfakes</li>
                <li>Uploading illegal or abusive content</li>
                <li>Circumventing identity verification or KYC processes</li>
                <li>Transmitting malware</li>
                <li>Violating laws or regulations</li>
              </ul>
            </Section>

            <Section id="id-documents" title="7. Government & Official Identity Document Disclaimer">
              <div className="rounded-2xl border-2 border-amber-300 bg-amber-50 p-6">
                <h3 className="font-heading font-bold text-foreground mb-3 flex items-center gap-2 text-lg">
                  <AlertTriangle className="w-5 h-5 text-amber-600" />
                  Important Notice — Government and Official Identity Documents
                </h3>
                <p>
                  AI-generated or materially AI-altered images, including images produced by BetterSnap AI, should not
                  be used for passports, visas, immigration filings, government-issued ID cards, driver’s licenses,
                  national identity documents, or other official identity or legal documents unless the relevant
                  authority expressly permits the use of AI-generated photographs.
                </p>
                <ul className="list-disc pl-6 space-y-1 mt-3">
                  <li>
                    BetterSnap AI makes no representation or warranty that any generated image will satisfy the
                    technical specifications or policy requirements of any government agency, immigration authority,
                    professional licensing body, or institution.
                  </li>
                  <li>Using AI-generated images where not permitted may constitute fraud or misrepresentation.</li>
                  <li>You assume full responsibility for consequences arising from such use.</li>
                  <li>You must verify photo requirements with the relevant authority before submitting any image.</li>
                </ul>
              </div>
            </Section>

            <Section id="warranties" title="8. User Representations and Warranties">
              <p>You represent that:</p>
              <ul className="list-disc pl-6 space-y-1 mt-2">
                <li>You own uploaded photos or have the necessary rights.</li>
                <li>If a photo depicts a third party, you have obtained explicit informed consent.</li>
                <li>Your use of the service complies with applicable laws.</li>
                <li>You understand the limitations of AI-generated imagery.</li>
                <li>You have read the Government and Official Identity Document Disclaimer.</li>
              </ul>
            </Section>

            <Section id="ai-limits" title="9. AI Output Limitations & Beta Disclaimer">
              <ul className="list-disc pl-6 space-y-1">
                <li>AI outputs are probabilistic.</li>
                <li>Output quality may vary based on upload quality, lighting, angle, and photo content.</li>
                <li>BetterSnap AI does not guarantee a perfect likeness.</li>
                <li>BetterSnap AI does not guarantee acceptance by employers, institutions, platforms, or third parties.</li>
                <li>BetterSnap AI does not guarantee uninterrupted availability or consistent output quality.</li>
                <li>Beta features are provided as-is and may change or be discontinued.</li>
              </ul>
            </Section>

            <Section id="disclaimer" title="10. Disclaimer of Warranties">
              <p>The service and outputs are provided on an “as-is” and “as-available” basis.</p>
              <ul className="list-disc pl-6 space-y-1 mt-2">
                <li>BetterSnap AI does not guarantee the service will be uninterrupted, error-free, or secure.</li>
                <li>
                  BetterSnap AI does not guarantee any AI-generated image will be accepted by any government agency,
                  employer, or institution.
                </li>
              </ul>
            </Section>

            <Section id="liability" title="11. Limitation of Liability">
              <p>BetterSnap AI is not liable for:</p>
              <ul className="list-disc pl-6 space-y-1 mt-2">
                <li>Visa denials</li>
                <li>Delayed immigration applications</li>
                <li>Government RFEs caused by non-compliant photos</li>
                <li>Loss of data, profits, revenue, business, or professional opportunities</li>
                <li>Unauthorized access to or alteration of data</li>
                <li>Errors, inaccuracies, or imperfections in AI outputs</li>
              </ul>
              <div className="mt-4 rounded-xl bg-card border border-border p-5">
                <p className="font-semibold text-foreground">Liability cap</p>
                <p className="mt-1">
                  BetterSnap AI’s total liability shall not exceed the greater of: the total amount paid to BetterSnap
                  AI in the six months before the claim, or USD $50.00.
                </p>
              </div>
            </Section>

            <Section id="indemnification" title="12. Indemnification">
              <p>You agree to defend and hold BetterSnap AI harmless for claims arising from:</p>
              <ul className="list-disc pl-6 space-y-1 mt-2">
                <li>Your use of the service</li>
                <li>Breach of these Terms</li>
                <li>Violation of law</li>
                <li>Violation of third-party rights</li>
                <li>Submission of AI-generated images to government authorities without required disclosures</li>
                <li>Uploaded content that violates IP or privacy rights</li>
              </ul>
            </Section>

            <Section id="termination" title="13. Termination">
              <ul className="list-disc pl-6 space-y-1">
                <li>BetterSnap AI may suspend or terminate accounts for violations, suspected fraud, or inactivity.</li>
                <li>Upon termination, access stops.</li>
                <li>Unused subscription credits may be forfeited except as required by law.</li>
                <li>Account records may be retained for up to 90 days for legal compliance.</li>
                <li>You may terminate your account by contacting support.</li>
              </ul>
            </Section>

            <Section id="governing-law" title="14. Governing Law & Dispute Resolution">
              <ul className="list-disc pl-6 space-y-1">
                <li>These Terms are governed by the laws of the State of Florida, United States.</li>
                <li>Disputes shall be resolved by binding arbitration administered under the AAA Consumer Arbitration Rules.</li>
                <li>You and BetterSnap AI waive the right to participate in class actions.</li>
                <li>Either party may bring qualifying claims in small-claims court where applicable.</li>
              </ul>
            </Section>

            <Section id="changes" title="15. Changes to These Terms">
              <ul className="list-disc pl-6 space-y-1">
                <li>These Terms may be updated from time to time.</li>
                <li>
                  Material changes may be communicated by email, in-app notification, or website notice at least 14 days
                  before taking effect.
                </li>
                <li>Continued use after the effective date means acceptance.</li>
              </ul>
            </Section>

            <Section id="contact" title="16. Company Information & Contact">
              <div className="rounded-2xl border border-border bg-card p-6">
                <dl className="grid sm:grid-cols-2 gap-x-6 gap-y-4 text-sm">
                  <div>
                    <dt className="text-muted-foreground">Company</dt>
                    <dd className="font-medium">BetterSnap AI</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">State of Operation</dt>
                    <dd className="font-medium">Florida, United States</dd>
                  </div>
                  <div className="sm:col-span-2">
                    <dt className="text-muted-foreground">Mailing Address</dt>
                    <dd className="font-medium">Tallahassee, FL, United States</dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Support</dt>
                    <dd>
                      <a href="mailto:support@bettersnap.ai" className="text-primary hover:underline inline-flex items-center gap-1">
                        <Mail className="w-4 h-4" /> support@bettersnap.ai
                      </a>
                    </dd>
                  </div>
                  <div>
                    <dt className="text-muted-foreground">Legal / Compliance</dt>
                    <dd>
                      <a href="mailto:legal@bettersnap.ai" className="text-primary hover:underline inline-flex items-center gap-1">
                        <Mail className="w-4 h-4" /> legal@bettersnap.ai
                      </a>
                    </dd>
                  </div>
                </dl>
                <p className="mt-5 text-xs text-muted-foreground">
                  Business contact details may be updated as BetterSnap AI finalizes its official company information.
                </p>
              </div>
            </Section>

            <footer className="mt-8 pt-8 border-t border-border flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 text-sm text-muted-foreground">
              <Link to="/" className="hover:text-foreground transition-colors">
                ← Back to BetterSnap AI home
              </Link>
              <Link to="/privacy-policy" className="text-primary hover:underline">
                Read the Privacy Policy →
              </Link>
            </footer>
          </article>
        </div>

        {showTop && (
          <button
            onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}
            aria-label="Back to top"
            className="fixed bottom-6 right-6 print:hidden z-40 w-11 h-11 rounded-full gradient-cta text-primary-foreground shadow-lg flex items-center justify-center hover:scale-105 transition-transform"
          >
            <ArrowUp className="w-5 h-5" />
          </button>
        )}
      </main>
      <Footer />
    </PageShell>
  );
};

const Section = ({ id, title, children }: { id: string; title: string; children: React.ReactNode }) => (
  <section id={id} className="scroll-mt-24">
    <h2 className="text-2xl md:text-3xl font-heading font-bold mb-4 text-foreground">{title}</h2>
    <div className="text-foreground">{children}</div>
  </section>
);

export default Terms;
