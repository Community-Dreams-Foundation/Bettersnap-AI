import { useState } from "react";
import { Link } from "react-router-dom";
import { motion } from "framer-motion";
import {
  Mail,
  Phone,
  Clock,
  CalendarDays,
  ArrowLeft,
  Send,
  CheckCircle2,
  AlertCircle,
  Loader2,
  Paperclip,
} from "lucide-react";
import Navbar from "@/components/Navbar";
import PageShell from "@/components/PageShell";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const categories = [
  "Account and Login",
  "Photo Upload",
  "Headshot Generation",
  "Editing and Downloads",
  "Pricing and Credits",
  "Billing and Refunds",
  "Privacy and Data",
  "Technical Issue",
  "Other",
];

type FormStatus = "idle" | "loading" | "success" | "error";

interface FormData {
  fullName: string;
  email: string;
  category: string;
  subject: string;
  description: string;
  attachment: File | null;
}

interface FormErrors {
  fullName?: string;
  email?: string;
  category?: string;
  subject?: string;
  description?: string;
}

const ContactSupport = () => {
  const [formData, setFormData] = useState<FormData>({
    fullName: "",
    email: "",
    category: "",
    subject: "",
    description: "",
    attachment: null,
  });
  const [errors, setErrors] = useState<FormErrors>({});
  const [status, setStatus] = useState<FormStatus>("idle");
  const [attachmentName, setAttachmentName] = useState<string>("");

  const validate = (): boolean => {
    const newErrors: FormErrors = {};
    if (!formData.fullName.trim()) {
      newErrors.fullName = "Full name is required";
    }
    if (!formData.email.trim()) {
      newErrors.email = "Email address is required";
    } else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(formData.email)) {
      newErrors.email = "Please enter a valid email address";
    }
    if (!formData.category) {
      newErrors.category = "Please select a support category";
    }
    if (!formData.subject.trim()) {
      newErrors.subject = "Subject is required";
    }
    if (!formData.description.trim()) {
      newErrors.description = "Description is required";
    }
    setErrors(newErrors);
    return Object.keys(newErrors).length === 0;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!validate()) {
      setStatus("error");
      return;
    }
    setStatus("loading");
    setErrors({});

    // Simulate API submission
    await new Promise((resolve) => setTimeout(resolve, 1500));
    setStatus("success");
  };

  const handleChange = (field: keyof FormData, value: string) => {
    setFormData((prev) => ({ ...prev, [field]: value }));
    if (errors[field as keyof FormErrors]) {
      setErrors((prev) => ({ ...prev, [field]: undefined }));
    }
    if (status === "error") setStatus("idle");
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0] || null;
    setFormData((prev) => ({ ...prev, attachment: file }));
    setAttachmentName(file ? file.name : "");
  };

  return (
    <PageShell>
      <Navbar />
      <main className="pb-20 px-4">
        <div className="container mx-auto max-w-5xl">
          {/* Back link */}
          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.3 }}
          >
            <Link
              to="/"
              className="inline-flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground transition-colors mb-8"
            >
              <ArrowLeft className="w-4 h-4" aria-hidden="true" />
              Back to home
            </Link>
          </motion.div>

          {/* Header */}
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.4 }}
            className="text-center mb-14"
          >
            <h1 className="text-3xl md:text-4xl lg:text-5xl font-heading font-bold text-foreground mb-4 leading-tight">
              Contact BetterSnap AI Support
            </h1>
            <p className="text-muted-foreground text-lg max-w-2xl mx-auto leading-relaxed">
              Need help with your account, photo generation, billing, or another
              issue? Our support team is here to assist you.
            </p>
          </motion.div>

          {/* Contact Cards */}
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.4, delay: 0.1 }}
            className="grid md:grid-cols-2 gap-6 mb-14"
          >
            {/* Email Card */}
            <div className="rounded-2xl border border-border bg-white/80 shadow-sm p-8 hover:shadow-md transition-shadow">
              <div className="w-12 h-12 rounded-xl bg-primary/10 flex items-center justify-center mb-5">
                <Mail className="w-6 h-6 text-primary" aria-hidden="true" />
              </div>
              <h2 className="text-xl font-heading font-bold text-foreground mb-2">
                Email Us
              </h2>
              <a
                href="mailto:xxxxxsupport@bettersnapai.com"
                className="text-primary font-medium hover:underline underline-offset-4 inline-flex items-center gap-2 mb-3"
              >
                <Mail className="w-4 h-4" aria-hidden="true" />
                xxxxxsupport@bettersnapai.com
              </a>
              <p className="text-muted-foreground text-sm leading-relaxed">
                Send us an email with a description of your issue, and include
                any relevant screenshots or details that may help us assist
                you.
              </p>
            </div>

            {/* Phone Card */}
            <div className="rounded-2xl border border-border bg-white/80 shadow-sm p-8 hover:shadow-md transition-shadow">
              <div className="w-12 h-12 rounded-xl bg-primary/10 flex items-center justify-center mb-5">
                <Phone className="w-6 h-6 text-primary" aria-hidden="true" />
              </div>
              <h2 className="text-xl font-heading font-bold text-foreground mb-2">
                Call Us
              </h2>
              <a
                href="tel:+1-XXX-XXX-XXXX"
                className="text-primary font-medium hover:underline underline-offset-4 inline-flex items-center gap-2 mb-3"
              >
                <Phone className="w-4 h-4" aria-hidden="true" />
                +1 (XXX) XXX-XXXX
              </a>
              <p className="text-muted-foreground text-sm leading-relaxed">
                Contact our support team during the available business hours
                listed below.
              </p>
            </div>
          </motion.div>

          {/* Response Time & Hours */}
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.4, delay: 0.2 }}
            className="grid md:grid-cols-2 gap-6 mb-14"
          >
            <div className="rounded-2xl border border-border bg-white/80 shadow-sm p-8">
              <div className="w-12 h-12 rounded-xl bg-primary/10 flex items-center justify-center mb-5">
                <Clock className="w-6 h-6 text-primary" aria-hidden="true" />
              </div>
              <h2 className="text-xl font-heading font-bold text-foreground mb-3">
                When will we respond?
              </h2>
              <p className="text-muted-foreground leading-relaxed">
                We will review your request and get back to you as soon as
                possible. Most support requests are answered within 1–2 business
                days.
              </p>
            </div>

            <div className="rounded-2xl border border-border bg-white/80 shadow-sm p-8">
              <div className="w-12 h-12 rounded-xl bg-primary/10 flex items-center justify-center mb-5">
                <CalendarDays
                  className="w-6 h-6 text-primary"
                  aria-hidden="true"
                />
              </div>
              <h2 className="text-xl font-heading font-bold text-foreground mb-3">
                Support Hours
              </h2>
              <p className="text-muted-foreground leading-relaxed mb-2">
                <span className="font-medium text-foreground">
                  Monday–Friday:
                </span>{" "}
                9:00 AM–5:00 PM Eastern Time
              </p>
              <p className="text-muted-foreground leading-relaxed mb-3">
                <span className="font-medium text-foreground">
                  Saturday, Sunday, and U.S. federal holidays:
                </span>{" "}
                Closed
              </p>
              <p className="text-sm text-muted-foreground italic">
                Messages received outside business hours will be reviewed on the
                next available business day.
              </p>
            </div>
          </motion.div>

          {/* Support Form */}
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.4, delay: 0.3 }}
            className="rounded-2xl border border-border bg-white/80 shadow-sm p-8 md:p-10 mb-14"
          >
            <h2 className="text-2xl font-heading font-bold text-foreground mb-2">
              Send a Support Request
            </h2>
            <p className="text-muted-foreground mb-8">
              Fill out the form below and we will get back to you as soon as
              possible.
            </p>

            {status === "success" ? (
              <div
                className="rounded-xl bg-green-50 border border-green-200 p-6 text-center"
                role="alert"
                aria-live="polite"
              >
                <CheckCircle2
                  className="w-10 h-10 text-green-600 mx-auto mb-3"
                  aria-hidden="true"
                />
                <h3 className="text-lg font-semibold text-green-800 mb-2">
                  Request Received
                </h3>
                <p className="text-green-700 max-w-md mx-auto">
                  Your request has been received. Our support team will respond
                  as soon as possible, usually within 1–2 business days.
                </p>
              </div>
            ) : (
              <form onSubmit={handleSubmit} className="space-y-6" noValidate>
                <div className="grid md:grid-cols-2 gap-6">
                  {/* Full Name */}
                  <div className="space-y-2">
                    <Label htmlFor="fullName">Full Name</Label>
                    <Input
                      id="fullName"
                      type="text"
                      placeholder="Your full name"
                      value={formData.fullName}
                      onChange={(e) =>
                        handleChange("fullName", e.target.value)
                      }
                      aria-invalid={!!errors.fullName}
                      aria-describedby={
                        errors.fullName ? "fullName-error" : undefined
                      }
                    />
                    {errors.fullName && (
                      <p id="fullName-error" className="text-sm text-destructive flex items-center gap-1">
                        <AlertCircle className="w-3.5 h-3.5" aria-hidden="true" />
                        {errors.fullName}
                      </p>
                    )}
                  </div>

                  {/* Email */}
                  <div className="space-y-2">
                    <Label htmlFor="email">Email Address</Label>
                    <Input
                      id="email"
                      type="email"
                      placeholder="you@example.com"
                      value={formData.email}
                      onChange={(e) =>
                        handleChange("email", e.target.value)
                      }
                      aria-invalid={!!errors.email}
                      aria-describedby={
                        errors.email ? "email-error" : undefined
                      }
                    />
                    {errors.email && (
                      <p id="email-error" className="text-sm text-destructive flex items-center gap-1">
                        <AlertCircle className="w-3.5 h-3.5" aria-hidden="true" />
                        {errors.email}
                      </p>
                    )}
                  </div>
                </div>

                <div className="grid md:grid-cols-2 gap-6">
                  {/* Category */}
                  <div className="space-y-2">
                    <Label htmlFor="category">Support Category</Label>
                    <Select
                      value={formData.category}
                      onValueChange={(value) =>
                        handleChange("category", value)
                      }
                    >
                      <SelectTrigger
                        id="category"
                        aria-invalid={!!errors.category}
                        aria-describedby={
                          errors.category ? "category-error" : undefined
                        }
                      >
                        <SelectValue placeholder="Select a category" />
                      </SelectTrigger>
                      <SelectContent>
                        {categories.map((cat) => (
                          <SelectItem key={cat} value={cat}>
                            {cat}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    {errors.category && (
                      <p id="category-error" className="text-sm text-destructive flex items-center gap-1">
                        <AlertCircle className="w-3.5 h-3.5" aria-hidden="true" />
                        {errors.category}
                      </p>
                    )}
                  </div>

                  {/* Subject */}
                  <div className="space-y-2">
                    <Label htmlFor="subject">Subject</Label>
                    <Input
                      id="subject"
                      type="text"
                      placeholder="Brief subject of your request"
                      value={formData.subject}
                      onChange={(e) =>
                        handleChange("subject", e.target.value)
                      }
                      aria-invalid={!!errors.subject}
                      aria-describedby={
                        errors.subject ? "subject-error" : undefined
                      }
                    />
                    {errors.subject && (
                      <p id="subject-error" className="text-sm text-destructive flex items-center gap-1">
                        <AlertCircle className="w-3.5 h-3.5" aria-hidden="true" />
                        {errors.subject}
                      </p>
                    )}
                  </div>
                </div>

                {/* Description */}
                <div className="space-y-2">
                  <Label htmlFor="description">Description</Label>
                  <Textarea
                    id="description"
                    placeholder="Describe your issue in detail..."
                    rows={5}
                    value={formData.description}
                    onChange={(e) =>
                      handleChange("description", e.target.value)
                    }
                    aria-invalid={!!errors.description}
                    aria-describedby={
                      errors.description ? "description-error" : undefined
                    }
                  />
                  {errors.description && (
                    <p id="description-error" className="text-sm text-destructive flex items-center gap-1">
                      <AlertCircle className="w-3.5 h-3.5" aria-hidden="true" />
                      {errors.description}
                    </p>
                  )}
                </div>

                {/* Attachment */}
                <div className="space-y-2">
                  <Label htmlFor="attachment">
                    Optional Screenshot or Attachment
                  </Label>
                  <div className="flex items-center gap-3">
                    <label
                      htmlFor="attachment"
                      className="inline-flex items-center gap-2 px-4 py-2.5 rounded-lg border border-border bg-background text-sm font-medium text-foreground hover:bg-muted cursor-pointer transition-colors"
                    >
                      <Paperclip className="w-4 h-4" aria-hidden="true" />
                      Choose file
                    </label>
                    <input
                      id="attachment"
                      type="file"
                      accept="image/*,.pdf,.doc,.docx,.txt"
                      className="sr-only"
                      onChange={handleFileChange}
                    />
                    {attachmentName && (
                      <span className="text-sm text-muted-foreground truncate max-w-[200px]">
                        {attachmentName}
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-muted-foreground">
                    Accepted: images, PDF, DOC, DOCX, TXT (max 10 MB)
                  </p>
                </div>

                {/* Submit */}
                <div className="pt-2">
                  <Button
                    type="submit"
                    disabled={status === "loading"}
                    className="w-full md:w-auto inline-flex items-center gap-2 px-8 py-3 h-auto rounded-xl gradient-cta text-primary-foreground font-semibold text-base hover-scale btn-glow shadow-lg"
                  >
                    {status === "loading" ? (
                      <>
                        <Loader2
                          className="w-5 h-5 animate-spin"
                          aria-hidden="true"
                        />
                        Sending...
                      </>
                    ) : (
                      <>
                        <Send className="w-5 h-5" aria-hidden="true" />
                        Send Support Request
                      </>
                    )}
                  </Button>
                </div>
              </form>
            )}
          </motion.div>

          {/* Placeholder Notice */}
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ duration: 0.4, delay: 0.4 }}
            className="rounded-xl border border-border bg-white/60 p-6 text-center"
          >
            <p className="text-sm text-muted-foreground leading-relaxed">
              Support contact details and business hours shown on this page are
              temporary placeholders and will be updated once the official
              BetterSnap AI support information is finalized.
            </p>
          </motion.div>
        </div>
      </main>
    </PageShell>
  );
};

export default ContactSupport;
