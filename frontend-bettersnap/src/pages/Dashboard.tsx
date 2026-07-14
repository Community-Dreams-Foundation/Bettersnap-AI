import { useState, useEffect } from "react";
import Navbar from "@/components/Navbar";
import PageShell from "@/components/PageShell";
import { motion } from "framer-motion";
import {
  Download,
  Loader2,
  ImageOff,
  Image as ImageIcon,
  Zap,
  ShieldCheck,
  AlertCircle,
  CheckCircle2,
  Clock,
} from "lucide-react";
import { Link } from "react-router-dom";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  getUserJobs,
  getUserCredits,
  getJobResultUrls,
  type AzureJob,
  type JobStatus,
} from "@/lib/azureApi";
import { downloadImage } from "@/lib/download";
import { useAuth } from "@/contexts/AuthContext";

interface RecentGeneration extends AzureJob {
  imageUrls: string[];
  imageCount: number;
}

const statusMeta: Record<JobStatus, { label: string; className: string }> = {
  waiting_lora: { label: "Waiting for model", className: "bg-amber-500/10 text-amber-600 border-amber-500/20" },
  queued: { label: "Queued", className: "bg-blue-500/10 text-blue-600 border-blue-500/20" },
  dispatching: { label: "Dispatching", className: "bg-blue-500/10 text-blue-600 border-blue-500/20" },
  processing: { label: "Processing", className: "bg-blue-500/10 text-blue-600 border-blue-500/20" },
  completed: { label: "Completed", className: "bg-green-500/10 text-green-600 border-green-500/20" },
  failed: { label: "Failed", className: "bg-destructive/10 text-destructive border-destructive/20" },
};

const formatDate = (iso: string) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
};

const Dashboard = () => {
  const { user } = useAuth();
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [credits, setCredits] = useState<number>(0);
  const [totalImages, setTotalImages] = useState<number>(0);
  const [recent, setRecent] = useState<RecentGeneration[]>([]);

  const displayName = user?.name || user?.username || "there";

  useEffect(() => {
    if (!user) return;
    let cancelled = false;

    const load = async () => {
      setLoading(true);
      setLoadError(null);
      try {
        // Credits: GET /users/credits -> { credits_remaining }
        const [{ credits_remaining }, jobs] = await Promise.all([getUserCredits(), getUserJobs()]);
        if (cancelled) return;
        setCredits(credits_remaining ?? 0);

        const completed = jobs.filter((j) => j.status === "completed");

        // "Photos Generated" = sum of ALL images across completed jobs, not job count.
        const imageTotal = completed.reduce(
          (sum, j) => sum + (Array.isArray(j.output_blob_path) ? j.output_blob_path.length : 0),
          0,
        );
        setTotalImages(imageTotal);

        // Recent generations: show latest 6 jobs (completed or in-flight), fetch
        // thumbnail URLs only for completed ones to avoid unnecessary requests.
        const sorted = [...jobs].sort(
          (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
        );
        const latest = sorted.slice(0, 6);

        const withUrls: RecentGeneration[] = await Promise.all(
          latest.map(async (job) => {
            const count = Array.isArray(job.output_blob_path) ? job.output_blob_path.length : 0;
            if (job.status !== "completed") {
              return { ...job, imageUrls: [], imageCount: count };
            }
            try {
              const urls = await getJobResultUrls(job.job_id);
              return { ...job, imageUrls: urls, imageCount: urls.length || count };
            } catch {
              return { ...job, imageUrls: [], imageCount: count };
            }
          }),
        );
        if (!cancelled) setRecent(withUrls);
      } catch (err: any) {
        if (!cancelled) setLoadError(err?.message ?? "Could not load your dashboard. Please try again.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    load();
    return () => {
      cancelled = true;
    };
  }, [user]);

  const stats = [
    { icon: ImageIcon, label: "Photos Generated", value: totalImages > 0 ? String(totalImages) : "—" },
    { icon: Zap, label: "Credits Remaining", value: String(credits) },
    { icon: Clock, label: "Avg. Speed", value: "<2 min" },
  ];

  return (
    <PageShell>
      <Navbar />
      <div className="container mx-auto px-4 pb-16 page-transition">
        <div className="mb-8">
          <div className="flex flex-wrap items-center gap-3 mb-3">
            <p className="text-lg font-medium text-muted-foreground">Hello, {displayName}</p>
            <Link
              to="/billing"
              className="inline-flex items-center gap-1.5 bg-primary/10 text-primary border border-primary/20 rounded-full px-3 py-1 text-sm font-medium hover:bg-primary/20 transition-colors"
              aria-label="View billing and credits"
            >
              <Zap className="w-3.5 h-3.5" aria-hidden="true" />
              {credits} credits left
            </Link>
          </div>
          <h1 className="text-3xl font-heading font-bold text-foreground mb-2">Your Dashboard</h1>
          <p className="text-muted-foreground">Your recent generations and account status at a glance.</p>
        </div>

        {/* Stats */}
        <div className="grid grid-cols-3 gap-4 mb-8">
          {stats.map((s) => (
            <div key={s.label} className="glass rounded-2xl p-4 shadow-glass text-center">
              <s.icon className="w-5 h-5 text-primary mx-auto mb-2" aria-hidden="true" />
              <p className="text-2xl font-heading font-bold text-foreground">{s.value}</p>
              <p className="text-xs text-muted-foreground">{s.label}</p>
            </div>
          ))}
        </div>

        {loadError && (
          <div
            role="alert"
            className="mb-6 flex items-start gap-2 rounded-xl border border-destructive/40 bg-destructive/5 p-4"
          >
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
            <p className="text-sm text-destructive">{loadError}</p>
          </div>
        )}

        <div className="grid lg:grid-cols-3 gap-6">
          <div className="lg:col-span-2">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-xl font-heading font-semibold text-foreground">Recent Generations</h2>
              <Link to="/history" className="text-sm text-primary hover:underline">
                View all
              </Link>
            </div>

            {loading ? (
              <div className="flex items-center justify-center py-20" role="status" aria-label="Loading">
                <Loader2 className="w-8 h-8 text-primary animate-spin" />
              </div>
            ) : recent.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-16 text-center glass rounded-2xl shadow-glass">
                <ImageOff className="w-12 h-12 text-muted-foreground mb-4" aria-hidden="true" />
                <p className="text-foreground font-medium">No generations yet</p>
                <p className="text-sm text-muted-foreground mt-1 mb-4">
                  Train your model and create your first headshots.
                </p>
                <Button asChild className="gradient-cta text-primary-foreground">
                  <Link to="/onboarding">Get started</Link>
                </Button>
              </div>
            ) : (
              <div className="space-y-4">
                {recent.map((job, i) => {
                  const meta = statusMeta[job.status] ?? statusMeta.queued;
                  return (
                    <motion.article
                      key={job.job_id}
                      initial={{ opacity: 0, y: 12 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ delay: i * 0.05 }}
                      className="glass rounded-2xl p-4 shadow-glass"
                    >
                      <header className="flex items-center justify-between gap-3 mb-3">
                        <div className="flex items-center gap-2 min-w-0">
                          <Badge variant="outline" className={`text-xs ${meta.className}`}>
                            {job.status === "completed" ? (
                              <CheckCircle2 className="w-3 h-3 mr-1" aria-hidden="true" />
                            ) : job.status === "failed" ? (
                              <AlertCircle className="w-3 h-3 mr-1" aria-hidden="true" />
                            ) : (
                              <Loader2 className="w-3 h-3 mr-1 animate-spin" aria-hidden="true" />
                            )}
                            {meta.label}
                          </Badge>
                          <span className="text-xs text-muted-foreground truncate">{formatDate(job.created_at)}</span>
                        </div>
                        <span className="text-xs text-muted-foreground shrink-0">
                          {job.imageCount} {job.imageCount === 1 ? "image" : "images"}
                        </span>
                      </header>

                      {job.imageUrls.length > 0 ? (
                        <div className="grid grid-cols-4 gap-2">
                          {job.imageUrls.slice(0, 4).map((url, idx) => (
                            <div
                              key={`${job.job_id}-${idx}`}
                              className="relative aspect-square overflow-hidden rounded-lg bg-muted"
                            >
                              <img
                                src={url}
                                alt={`Generation ${idx + 1}`}
                                loading="lazy"
                                className="w-full h-full object-cover"
                              />
                              <button
                                type="button"
                                onClick={() =>
                                  downloadImage(url, `bettersnap-${job.job_id.slice(0, 8)}-${idx + 1}.jpg`)
                                }
                                className="absolute inset-0 flex items-center justify-center bg-black/0 hover:bg-black/40 text-transparent hover:text-white transition"
                                aria-label={`Download image ${idx + 1}`}
                              >
                                <Download className="w-5 h-5" />
                              </button>
                            </div>
                          ))}
                        </div>
                      ) : job.status === "completed" ? (
                        <p className="text-xs text-muted-foreground">Images unavailable.</p>
                      ) : (
                        <p className="text-xs text-muted-foreground">
                          {job.status === "waiting_lora"
                            ? "Waiting for your model to finish training…"
                            : "Working on it…"}
                        </p>
                      )}
                    </motion.article>
                  );
                })}
              </div>
            )}
          </div>

          <aside className="space-y-4">
            <div className="glass rounded-2xl p-6 shadow-glass">
              <div className="w-10 h-10 rounded-full bg-primary/10 flex items-center justify-center mb-3">
                <ShieldCheck className="w-5 h-5 text-primary" aria-hidden="true" />
              </div>
              <h3 className="font-heading font-semibold text-foreground mb-2">Your photos stay private</h3>
              <p className="text-sm text-muted-foreground">
                Uploaded photos are used only to build your personal model and generate your headshots. We do not
                display your images publicly.
              </p>
            </div>
            <div className="glass rounded-2xl p-6 shadow-glass">
              <h3 className="font-heading font-semibold text-foreground mb-2">Quick Tips</h3>
              <ul className="space-y-2 text-sm text-muted-foreground">
                <li className="flex items-start gap-2">
                  <span className="text-primary mt-0.5" aria-hidden="true">•</span>
                  Front-facing photos with even lighting train better models.
                </li>
                <li className="flex items-start gap-2">
                  <span className="text-primary mt-0.5" aria-hidden="true">•</span>
                  Mix expressions, angles, and outfits across your 8–12 photos.
                </li>
              </ul>
            </div>
          </aside>
        </div>
      </div>
    </PageShell>
  );
};

export default Dashboard;
