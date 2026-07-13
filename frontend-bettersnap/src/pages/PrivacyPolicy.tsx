import { Link } from "react-router-dom";
import { useEffect, useState } from "react";
import { ArrowLeft, ArrowUp, AlertTriangle, Mail, ShieldCheck, Lock, Database } from "lucide-react";
import PageShell from "@/components/PageShell";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

const sections = [
  { id: "introduction", title: "1. Introduction" },
  { id: "id-notice", title: "2. Government & Official ID Notice" },
  { id: "information-collected", title: "3. Information We Collect" },
  { id: "how-we-use", title: "4. How We Use Your Information" },
  { id: "ai-training", title: "5. AI Model Training — Explicit Consent" },
  { id: "biometric", title: "6. Biometric Privacy & State Laws" },
  { id: "retention", title: "7. Data Retention" },
  { id: "subprocessors", title: "8. Third-Party Services & Subprocessors" },
  { id: "security", title: "9. Data Security" },
  { id: "rights", title: "10. Your Privacy Rights" },
  { id: "marketing", title: "11. Marketing Communications" },
  { id: "children", title: "12. Children's Privacy" },
  { id: "international", title: "13. International Data Transfers" },
  { id: "compliance", title: "14. Compliance with Applicable Laws" },
  { id: "contact", title: "15. Support Contact and Data Requests" },
  { id: "changes", title: "16. Changes to This Privacy Policy" },
];

const cookieRows = [
  { type: "Strictly Necessary", purpose: "Authentication, session management, security", retention: "Session / up to 1 year" },
  { type: "Functional", purpose: "User preferences and language settings", retention: "Up to 1 year" },
  { type: "Analytics", purpose: "Aggregate usage patterns via privacy-respecting tools", retention: "Up to 2 years" },
  { type: "Marketing / Advertising", purpose: "No advertising cookies or third-party ad networks", retention: "N/A" },
];

const retentionRows = [
  { data: "Original uploaded photos", location: "RAM / Azure processing pipeline", retention: "Deleted within ~30–90 seconds after processing", trigger: "Automatic on job completion" },
  { data: "Face data / facial features", location: "Real-time processing only", retention: "Never persisted", trigger: "Not stored separately" },
  { data: "Generated images (server-side)", location: "Azure Blob Storage", retention: "30 to 60 days from creation", trigger: "Automatic deletion at retention limit or account deletion" },
  { data: "Generated images (user device)", location: "User device", retention: "Until user deletes them", trigger: "User action" },
  { data: "Account / profile data", location: "Azure SQL / CosmosDB", retention: "Duration of account + 90 days post-closure", trigger: "Account closure" },
  { data: "Payment records", location: "Stripe / internal records", retention: "7 years", trigger: "Tax / legal compliance" },
  { data: "Support communications", location: "Email / ticketing system", retention: "3 years", trigger: "Ticket closure" },
  { data: "Security / access logs", location: "Azure Monitor / Log Analytics", retention: "30 to 60 days", trigger: "Automatic rotation" },
];

const subprocessors = [
  { name: "FLUX.1-Kontext", purpose: "AI headshot and lifestyle generation", data: "Uploaded photos via API; not retained by vendor", location: "USA / EU" },
  { name: "Stripe", purpose: "Payment processing and subscription management", data: "Payment transaction data", location: "USA" },
  { name: "Microsoft Azure", purpose: "Cloud infrastructure, authentication, database, blob storage", data: "Account data, usage data, generated images", location: "USA" },
];

const rights = [
  { right: "Access", how: "Email privacy@bettersnap.ai" },
  { right: "Rectification", how: "Account settings or email privacy@bettersnap.ai" },
  { right: "Erasure / Right to be Forgotten", how: "Email privacy@bettersnap.ai" },
  { right: "Withdraw Biometric Consent", how: "Stop using photo features and email privacy@bettersnap.ai" },
  { right: "Opt-Out of Sale", how: "We do not sell personal data; email privacy@bettersnap.ai to confirm" },
  { right: "Data Portability", how: "Email privacy@bettersnap.ai" },
  { right: "Restrict Processing", how: "Email privacy@bettersnap.ai" },
  { right: "Opt-Out of Marketing", how: "Use unsubscribe link or account preferences" },
];

const contactRows = [
  { topic: "General Privacy Questions", email: "privacy@bettersnap.ai", response: "5 business days" },
  { topic: "Data Access / Deletion Requests", email: "privacy@bettersnap.ai", response: "Confirmation within 5 business days; completion within 30 days where applicable" },
  { topic: "Security Incidents", email: "security@bettersnap.ai", response: "Priority review within 24–72 hours" },
  { topic: "General Support", email: "support@bettersnap.ai", response: "2 business days" },
  { topic: "Legal / Compliance", email: "legal@bettersnap.ai", response: "5 business days" },
];

const complianceRows = [
  { law: "Illinois BIPA", approach: "Separate biometric consent, no sale of biometric data, written retention/destruction policy" },
  { law: "CCPA / CPRA", approach: "Data rights for California residents, no sale of personal information" },
  { law: "GDPR", approach: "Lawful basis, data rights, SCCs for transfers, DPAs with subprocessors" },
  { law: "UK GDPR", approach: "Equivalent data rights for UK residents, adequacy / SCCs where applicable" },
  { law: "Other U.S. state privacy laws", approach: "Equivalent data rights for residents of applicable states" },
];

const PrivacyPolicy = () => {
  const [activeId, setActiveId] = useState(sections[0].id);
  const [showTop, setShowTop] = useState(false);

  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "instant" as ScrollBehavior });
  }, []);

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
      <main className="container mx-auto px-4 pb-12 lg:pb-16 max-w-7xl print:py-4">
        <Link
          to="/"
          className="inline-flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground mt-8 mb-6 transition-colors print:hidden"
        >
          <ArrowLeft className="w-4 h-4" /> Back to home
        </Link>

        <header className="mb-8">
          <h1 className="text-4xl md:text-5xl font-heading font-bold text-foreground mb-3">Privacy Policy</h1>
          <p className="text-lg text-muted-foreground mb-2">
            How BetterSnap AI collects, uses, protects, and manages your data.
          </p>
          <p className="text-sm text-muted-foreground">Last updated: June 2026</p>
        </header>

        {/* Summary cards */}
        <div className="grid md:grid-cols-3 gap-4 mb-10">
          <SummaryCard icon={ShieldCheck} title="No model training without consent">
            We never use your photos or face data to train AI models unless you provide separate explicit opt-in consent.
          </SummaryCard>
          <SummaryCard icon={Lock} title="Encrypted in transit and at rest">
            TLS 1.2+ for data in transit and AES-256 encryption at rest. No advertising or data-broker sharing.
          </SummaryCard>
          <SummaryCard icon={Database} title="Minimal retention">
            Original uploads are deleted within ~30–90 seconds of processing. You can request deletion of generated
            images at any time.
          </SummaryCard>
        </div>

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
            <Section id="introduction" title="1. Introduction">
              <p>
                BetterSnap AI is committed to protecting your privacy and handling personal data with transparency and
                care. This Privacy Policy describes the information we collect, how we use it, who we share it with, and
                the rights you have over your data.
              </p>
            </Section>

            <Section id="id-notice" title="2. Government & Official Identity Document Notice">
              <div className="rounded-2xl border-2 border-amber-300 bg-amber-50 p-6">
                <h3 className="font-heading font-bold text-foreground mb-2 flex items-center gap-2 text-lg">
                  <AlertTriangle className="w-5 h-5 text-amber-600" />
                  AI-generated images and official documents
                </h3>
                <p>
                  AI-generated or materially AI-altered images should not be submitted for passports, visas, immigration
                  filings, government IDs, or official identity documents unless the relevant authority expressly
                  permits AI-generated photographs.
                </p>
              </div>
            </Section>

            <Section id="information-collected" title="3. Information We Collect">
              <SubCard title="A. Photos and Facial Data">
                <ul className="list-disc pl-6 space-y-1">
                  <li>Photos voluntarily uploaded by users.</li>
                  <li>Facial features analyzed by AI to generate headshots.</li>
                  <li>Facial recognition is not used to identify users beyond the requested generation.</li>
                  <li>Persistent biometric identifiers, faceprints, or facial geometry maps are not created or stored.</li>
                </ul>
              </SubCard>
              <SubCard title="B. Account Information">
                <ul className="list-disc pl-6 space-y-1">
                  <li>Email address</li>
                  <li>Name</li>
                  <li>Authentication credentials if you create an account</li>
                  <li>Government-issued ID is not required for standard use</li>
                </ul>
              </SubCard>
              <SubCard title="C. Payment Information">
                <ul className="list-disc pl-6 space-y-1">
                  <li>Payments are processed by Stripe.</li>
                  <li>BetterSnap AI does not directly collect or store full payment card numbers.</li>
                  <li>Stripe may share transaction metadata for support.</li>
                </ul>
              </SubCard>
              <SubCard title="D. Automatically Collected Technical Data">
                <ul className="list-disc pl-6 space-y-1">
                  <li>Device type</li>
                  <li>Operating system version</li>
                  <li>Application version</li>
                  <li>Usage data</li>
                  <li>IP address for security and fraud prevention</li>
                  <li>Crash reports and diagnostics</li>
                </ul>
              </SubCard>
              <SubCard title="E. Cookies and Analytics">
                <Table
                  headers={["Type", "Purpose", "Retention"]}
                  rows={cookieRows.map((r) => [r.type, r.purpose, r.retention])}
                />
              </SubCard>
            </Section>

            <Section id="how-we-use" title="4. How We Use Your Information">
              <ul className="list-disc pl-6 space-y-1">
                <li>Generating AI headshots and lifestyle images</li>
                <li>Processing payments and managing subscriptions or credits</li>
                <li>Providing customer support</li>
                <li>Detecting and preventing abuse, fraud, and unauthorized access</li>
                <li>Sending service-related communications</li>
                <li>Improving performance through aggregate anonymized analytics</li>
                <li>Complying with legal obligations</li>
              </ul>

              <div className="mt-6 rounded-2xl border border-border bg-card p-6">
                <h3 className="font-heading font-semibold text-foreground mb-3">What We Do NOT Do</h3>
                <ul className="list-disc pl-6 space-y-1">
                  <li>We do not use photos or facial data to train AI models unless you provide separate explicit opt-in consent.</li>
                  <li>We do not build facial recognition databases or biometric profiles.</li>
                  <li>We do not share photos or face data with advertisers, data brokers, or marketing partners.</li>
                  <li>We do not sell personal information.</li>
                </ul>
              </div>
            </Section>

            <Section id="ai-training" title="5. AI Model Training — Explicit Consent Required">
              <ul className="list-disc pl-6 space-y-1">
                <li>Uploaded photos or generated images will never be used to train, fine-tune, or improve AI models without separate explicit opt-in consent.</li>
                <li>Any model-improvement program must be voluntary, opt-in only, and separate from the Terms.</li>
                <li>
                  You may revoke consent at any time by contacting{" "}
                  <a className="text-primary hover:underline" href="mailto:support@bettersnap.ai">
                    support@bettersnap.ai
                  </a>
                  .
                </li>
                <li>If no separate model-training consent exists, your data is not used for model training.</li>
              </ul>
            </Section>

            <Section id="biometric" title="6. Biometric Privacy & Applicable State Laws">
              <ul className="list-disc pl-6 space-y-1">
                <li>For users in Illinois and similar jurisdictions, BetterSnap AI presents a clear biometric data consent notice before collecting or processing facial data.</li>
                <li>Biometric consent is separate from the general Terms.</li>
                <li>
                  You may withdraw biometric consent by ceasing use of photo-upload features and submitting deletion
                  requests to{" "}
                  <a className="text-primary hover:underline" href="mailto:privacy@bettersnap.ai">
                    privacy@bettersnap.ai
                  </a>
                  .
                </li>
                <li>
                  This notice references the Illinois Biometric Information Privacy Act (BIPA) and similar state laws.
                  It is provided for transparency and is not legal advice.
                </li>
              </ul>
            </Section>

            <Section id="retention" title="7. Data Retention">
              <Table
                headers={["Data Type", "Location", "Retention", "Trigger"]}
                rows={retentionRows.map((r) => [r.data, r.location, r.retention, r.trigger])}
              />
              <p className="mt-4">
                You may request earlier deletion, excluding legally required records, by contacting{" "}
                <a className="text-primary hover:underline" href="mailto:privacy@bettersnap.ai">
                  privacy@bettersnap.ai
                </a>
                . Verified deletion requests are processed within 30 days.
              </p>
            </Section>

            <Section id="subprocessors" title="8. Third-Party Services & Subprocessors">
              <Table
                headers={["Subprocessor", "Purpose", "Data Shared", "Location"]}
                rows={subprocessors.map((r) => [r.name, r.purpose, r.data, r.location])}
              />
            </Section>

            <Section id="security" title="9. Data Security">
              <ul className="list-disc pl-6 space-y-1">
                <li>TLS 1.2 or higher for data in transit.</li>
                <li>AES-256 encryption at rest for Azure Blob Storage and databases.</li>
                <li>RBAC limits employee and system access on a need-to-know basis.</li>
                <li>Original uploaded photos are not permanently retained.</li>
                <li>Periodic security assessments and patching.</li>
                <li>Breach notifications as required by law.</li>
                <li>
                  Security incidents contact:{" "}
                  <a className="text-primary hover:underline" href="mailto:security@bettersnap.ai">
                    security@bettersnap.ai
                  </a>
                  .
                </li>
              </ul>
            </Section>

            <Section id="rights" title="10. Your Privacy Rights">
              <Table headers={["Right", "How to Exercise"]} rows={rights.map((r) => [r.right, r.how])} />
              <h3 className="font-heading font-semibold text-foreground mt-6 mb-2">Deletion timeline</h3>
              <ul className="list-disc pl-6 space-y-1">
                <li>Confirm deletion request within 5 business days.</li>
                <li>Complete deletion within 30 days, excluding legally retained records.</li>
                <li>Send written confirmation upon completion.</li>
              </ul>
            </Section>

            <Section id="marketing" title="11. Marketing Communications">
              <ul className="list-disc pl-6 space-y-1">
                <li>Marketing emails may be sent only if you opted in or where permitted by law.</li>
                <li>You may opt out by clicking the unsubscribe link, emailing privacy@bettersnap.ai, or updating account notification preferences.</li>
                <li>Opting out does not affect service-related messages.</li>
              </ul>
            </Section>

            <Section id="children" title="12. Children's Privacy">
              <ul className="list-disc pl-6 space-y-1">
                <li>The service is intended only for individuals 18 or older.</li>
                <li>BetterSnap AI does not knowingly collect data from anyone under 18.</li>
                <li>If data from a minor is discovered, it will be deleted and the account terminated.</li>
                <li>
                  Contact{" "}
                  <a className="text-primary hover:underline" href="mailto:privacy@bettersnap.ai">
                    privacy@bettersnap.ai
                  </a>{" "}
                  for concerns.
                </li>
              </ul>
            </Section>

            <Section id="international" title="13. International Data Transfers">
              <ul className="list-disc pl-6 space-y-1">
                <li>BetterSnap AI is operated from the United States.</li>
                <li>Users outside the United States acknowledge data transfer and processing in the United States.</li>
                <li>For EEA, UK, and Switzerland users, we rely on Standard Contractual Clauses and adequacy decisions where applicable.</li>
              </ul>
            </Section>

            <Section id="compliance" title="14. Compliance with Applicable Privacy Laws">
              <Table headers={["Law", "Our Approach"]} rows={complianceRows.map((r) => [r.law, r.approach])} />
            </Section>

            <Section id="contact" title="15. Support Contact and Data Requests">
              <Table
                headers={["Topic", "Contact", "Response Time"]}
                rows={contactRows.map((r) => [r.topic, r.email, r.response])}
              />
              <div className="mt-6 rounded-2xl border border-border bg-card p-6">
                <p className="text-sm text-muted-foreground mb-1">Mailing address</p>
                <address className="not-italic font-medium leading-relaxed">
                  BetterSnap AI
                  <br />
                  Tallahassee, FL, United States
                </address>
                <p className="mt-4 text-xs text-muted-foreground">
                  Business contact details may be updated as BetterSnap AI finalizes its official company information.
                </p>
              </div>
            </Section>

            <Section id="changes" title="16. Changes to This Privacy Policy">
              <ul className="list-disc pl-6 space-y-1">
                <li>This Privacy Policy may change.</li>
                <li>Significant changes may be notified by email, in-app notice, or prominent website notice.</li>
                <li>The Last Updated date will be revised.</li>
                <li>Material changes to personal-data use may require affirmative consent where required by law.</li>
              </ul>
            </Section>

            <footer className="mt-8 pt-8 border-t border-border flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4 text-sm text-muted-foreground">
              <Link to="/" className="hover:text-foreground transition-colors">
                ← Back to BetterSnap AI home
              </Link>
              <a
                href="mailto:privacy@bettersnap.ai"
                className="hover:text-foreground transition-colors inline-flex items-center gap-1"
              >
                <Mail className="w-4 h-4" /> privacy@bettersnap.ai
              </a>
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

const SubCard = ({ title, children }: { title: string; children: React.ReactNode }) => (
  <div className="rounded-2xl border border-border bg-card p-5 mb-4">
    <h3 className="font-heading font-semibold text-foreground mb-3">{title}</h3>
    {children}
  </div>
);

const SummaryCard = ({
  icon: Icon,
  title,
  children,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  children: React.ReactNode;
}) => (
  <div className="rounded-2xl p-6 bg-card border border-border shadow-sm">
    <div className="w-10 h-10 rounded-xl bg-primary/10 text-primary flex items-center justify-center mb-3">
      <Icon className="w-5 h-5" />
    </div>
    <h2 className="font-heading font-semibold text-foreground mb-2">{title}</h2>
    <p className="text-sm text-muted-foreground">{children}</p>
  </div>
);

const Table = ({ headers, rows }: { headers: string[]; rows: string[][] }) => (
  <div className="overflow-x-auto rounded-xl border border-border">
    <table className="w-full text-sm">
      <thead className="bg-muted/50">
        <tr className="text-left">
          {headers.map((h) => (
            <th key={h} className="px-4 py-3 font-semibold">
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody className="divide-y divide-border">
        {rows.map((r, i) => (
          <tr key={i}>
            {r.map((c, j) => (
              <td key={j} className="px-4 py-3 align-top text-muted-foreground">
                {c}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  </div>
);

export default PrivacyPolicy;
