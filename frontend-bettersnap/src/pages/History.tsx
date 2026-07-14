import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getUserJobs, getJobResultUrls, deleteJob, type AzureJob } from "@/lib/azureApi";
import { useAuth } from "@/contexts/AuthContext";
import Navbar from "@/components/Navbar";
import PageShell from "@/components/PageShell";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  Loader2,
  ImageOff,
  Download,
  RotateCcw,
  Calendar,
  Filter,
  Images,
  ChevronRight,
  ChevronLeft,
  Trash2,
  Check,
  AlertCircle,
} from "lucide-react";
import { downloadImage } from "@/lib/download";
import { motion, AnimatePresence } from "framer-motion";
import { toast } from "sonner";


interface HeadshotJob {
  id: string;
  session_id: string | null;
  output_image_url: string;
  category: string | null;
  background: string;
  gender: string;
  created_at: string;
}

interface Session {
  sessionId: string;
  category: string | null;
  background: string;
  gender: string;
  createdAt: string;
  jobs: HeadshotJob[];
}

interface Album {
  category: string | null;
  label: string;
  sessions: Session[];
  totalPhotos: number;
  coverUrl: string;
  latestCreatedAt: string;
}

const categoryLabels: Record<string, string> = {
  linkedin: "LinkedIn",
  resume: "Resume",
  university_id: "University ID",
};

const filterOptions: { value: string; label: string }[] = [
  { value: "all", label: "All" },
  { value: "linkedin", label: "LinkedIn" },
  { value: "resume", label: "Resume" },
  { value: "university_id", label: "University ID" },
];

const History = () => {
  const [jobs, setJobs] = useState<HeadshotJob[]>([]);
  const [brokenIds, setBrokenIds] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loggedIn, setLoggedIn] = useState<boolean | null>(null);
  const [filter, setFilter] = useState<string>("all");
  const [openSession, setOpenSession] = useState<Session | null>(null);
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [expandedCategory, setExpandedCategory] = useState<string | null>(null);
  const navigate = useNavigate();

  const { user } = useAuth();

  useEffect(() => {
    const fetchHistory = async () => {
      if (!user) {
        setLoggedIn(false);
        setLoading(false);
        return;
      }
      setLoggedIn(true);

      try {
        setLoadError(null);
        // getUserJobs() unwraps { jobs: [...] } from GET /users/jobs.
        const jobs = await getUserJobs();

        // Each job IS a session — fetch its output URLs and flatten into
        // per-image entries so the existing session/album UI keeps working.
        const jobsWithUrls = await Promise.all(
          jobs.map(async (job) => {
            if (job.status !== "completed") return { ...job, imageUrls: [] as string[] };
            try {
              const urls = await getJobResultUrls(job.job_id);
              return { ...job, imageUrls: urls };
            } catch {
              return { ...job, imageUrls: [] as string[] };
            }
          }),
        );

        const flattened: HeadshotJob[] = jobsWithUrls
          .filter((j) => j.imageUrls.length > 0)
          .flatMap((j) =>
            j.imageUrls.map((url, idx) => ({
              id: `${j.job_id}-${idx}`,
              session_id: j.job_id,
              output_image_url: url,
              category: j.category ?? null,
              background: (j.job_params?.background as string) ?? "",
              gender: (j.job_params?.gender as string) ?? "",
              created_at: j.created_at ?? "",
            })),
          )
          .sort((a, b) => b.created_at.localeCompare(a.created_at));

        setJobs(flattened);
      } catch (err: any) {
        console.error("Failed to load history:", err);
        setLoadError(err?.message ?? "Could not load your history. Please try again.");
      } finally {
        setLoading(false);
      }
    };
    fetchHistory();
  }, [user]);

  const sessions = useMemo<Session[]>(() => {
    const map = new Map<string, Session>();
    for (const job of jobs) {
      if (brokenIds.has(job.id)) continue;
      const key = job.session_id || job.id;
      const existing = map.get(key);
      if (existing) {
        existing.jobs.push(job);
        if (job.created_at < existing.createdAt) existing.createdAt = job.created_at;
      } else {
        map.set(key, {
          sessionId: key,
          category: job.category,
          background: job.background,
          gender: job.gender,
          createdAt: job.created_at,
          jobs: [job],
        });
      }
    }
    return Array.from(map.values()).sort((a, b) => b.createdAt.localeCompare(a.createdAt));
  }, [jobs, brokenIds]);

  const filteredSessions = filter === "all" ? sessions : sessions.filter((s) => s.category === filter);

  const albums = useMemo<Album[]>(() => {
    const map = new Map<string, Album>();
    for (const s of filteredSessions) {
      const validJobs = s.jobs.filter((j) => !brokenIds.has(j.id));
      if (validJobs.length === 0) continue;
      const key = s.category || "other";
      const existing = map.get(key);
      if (existing) {
        existing.sessions.push(s);
        existing.totalPhotos += validJobs.length;
        if (s.createdAt > existing.latestCreatedAt) {
          existing.latestCreatedAt = s.createdAt;
          existing.coverUrl = validJobs[0].output_image_url;
        }
      } else {
        map.set(key, {
          category: s.category,
          label: categoryLabels[s.category || ""] || "Other",
          sessions: [s],
          totalPhotos: validJobs.length,
          coverUrl: validJobs[0].output_image_url,
          latestCreatedAt: s.createdAt,
        });
      }
    }
    return Array.from(map.values())
      .filter((a) => a.totalPhotos > 0)
      .sort((a, b) => b.latestCreatedAt.localeCompare(a.latestCreatedAt));
  }, [filteredSessions, brokenIds]);

  const handleImageError = (jobId: string) => {
    setBrokenIds((prev) => new Set(prev).add(jobId));
  };

  const handleDownload = (url: string, category: string | null, idx?: number) => {
    const suffix = typeof idx === "number" ? `-${idx + 1}` : "";
    downloadImage(url, `headshot-${category || "photo"}${suffix}.jpg`);
  };

  const handleRedo = (s: Session) => {
    navigate("/generate", {
      state: { category: s.category, background: s.background, gender: s.gender },
    });
  };

  const deleteSession = async (jobId: string) => {
    setDeleting(true);
    try {
      await deleteJob(jobId);
      setJobs((prev) => prev.filter((j) => j.session_id !== jobId));
      if (openSession?.sessionId === jobId) setOpenSession(null);
      toast.success("Deleted successfully");
    } catch (err) {
      console.error("Failed to delete session:", err);
      toast.error("Delete failed");
    } finally {
      setDeleting(false);
      setPendingDeleteId(null);
    }
  };

  const formatDate = (dateStr: string) =>
    new Date(dateStr).toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      year: "numeric",
    });

  const formatTime = (dateStr: string) =>
    new Date(dateStr).toLocaleTimeString("en-US", {
      hour: "2-digit",
      minute: "2-digit",
    });

  const expandedAlbum = albums.find((a) => (a.category || "other") === expandedCategory) || null;

  return (
    <PageShell>
      <Navbar />
      <div className="pb-16 container mx-auto px-4 max-w-3xl page-transition">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 mb-8">
          <div>
            <h1 className="text-3xl font-heading font-bold text-foreground">
              Headshot <span className="text-gradient">History</span>
            </h1>
            <p className="text-muted-foreground mt-1">All your generation sessions in one place.</p>
          </div>

          {sessions.length > 0 && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="outline"
                  size="sm"
                  className="glass border-border text-foreground gap-2"
                  aria-label="Filter by category"
                >
                  <Filter className="w-4 h-4" />
                  {filterOptions.find((o) => o.value === filter)?.label || "All"}
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="glass">
                {filterOptions.map((opt) => (
                  <DropdownMenuItem
                    key={opt.value}
                    onSelect={() => {
                      setFilter(opt.value);
                      setExpandedCategory(null);
                    }}
                    className="flex items-center justify-between gap-4 cursor-pointer"
                  >
                    <span>{opt.label}</span>
                    {filter === opt.value && <Check className="w-4 h-4 text-primary" />}
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          )}
        </div>

        {loading && (
          <div className="flex justify-center py-16" role="status" aria-label="Loading history">
            <Loader2 className="w-8 h-8 text-primary animate-spin" />
          </div>
        )}

        {!loading && loggedIn === false && (
          <div className="text-center py-16 glass rounded-2xl shadow-glass">
            <p className="text-muted-foreground">Please log in to view your history.</p>
          </div>
        )}

        {!loading && loggedIn && loadError && (
          <div
            role="alert"
            className="mb-6 flex items-start gap-2 rounded-xl border border-destructive/40 bg-destructive/5 p-4"
          >
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
            <div>
              <p className="text-sm font-medium text-destructive">Couldn't load your history</p>
              <p className="text-xs text-destructive/80 mt-1">{loadError}</p>
            </div>
          </div>
        )}

        {!loading && loggedIn && albums.length === 0 && (
          <div className="text-center py-16 glass rounded-2xl shadow-glass">
            <ImageOff className="w-12 h-12 text-muted-foreground mx-auto mb-4" aria-hidden="true" />
            <p className="text-foreground font-medium">No headshots generated yet.</p>
            <p className="text-sm text-muted-foreground mt-1">Generate your first headshot to see it here.</p>
          </div>
        )}

        <AnimatePresence mode="wait">
          {expandedAlbum ? (
            <motion.div
              key="expanded"
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: 8 }}
              className="flex flex-col gap-3"
            >
              <div className="flex items-center justify-between mb-2">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setExpandedCategory(null)}
                  className="gap-1 text-muted-foreground hover:text-foreground"
                >
                  <ChevronLeft className="w-4 h-4" />
                  Back to albums
                </Button>
                <div className="flex items-center gap-2">
                  <h2 className="font-heading font-semibold text-foreground">{expandedAlbum.label}</h2>
                  <Badge variant="secondary" className="text-xs">
                    {expandedAlbum.sessions.length} {expandedAlbum.sessions.length === 1 ? "session" : "sessions"}
                  </Badge>
                </div>
              </div>

              {expandedAlbum.sessions.map((s, i) => {
                const thumb = s.jobs[0];
                const count = s.jobs.length;
                return (
                  <motion.div
                    key={s.sessionId}
                    initial={{ opacity: 0, y: 16 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: i * 0.04 }}
                    className="w-full glass rounded-2xl overflow-hidden shadow-glass hover:ring-1 hover:ring-primary/40 transition-all group"
                  >
                    <div className="flex items-center gap-4 p-4">
                      <button
                        type="button"
                        onClick={() => setOpenSession(s)}
                        className="flex items-center gap-4 flex-1 min-w-0 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-primary rounded-xl"
                        aria-label={`Open session with ${count} ${count === 1 ? "photo" : "photos"}`}
                      >
                        <div className="w-24 h-24 rounded-xl overflow-hidden flex-shrink-0 bg-muted">
                          <img
                            src={thumb.output_image_url}
                            alt={categoryLabels[s.category || ""] || "Headshot"}
                            className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500"
                            onError={() => handleImageError(thumb.id)}
                          />
                        </div>

                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2">
                            <h3 className="font-heading font-semibold text-foreground text-sm">
                              {categoryLabels[s.category || ""] || "Headshot"}
                            </h3>
                            <Badge className="gradient-cta text-primary-foreground border-0 text-[10px] px-1.5 py-0 gap-1">
                              <Images className="w-2.5 h-2.5" />
                              {count}
                            </Badge>
                          </div>
                          <div className="flex items-center gap-1 mt-0.5">
                            <Calendar className="w-3 h-3 text-muted-foreground" />
                            <p className="text-xs text-muted-foreground">
                              {formatDate(s.createdAt)} · {formatTime(s.createdAt)}
                            </p>
                          </div>
                        </div>

                        <ChevronRight className="w-4 h-4 text-muted-foreground group-hover:text-primary transition-colors flex-shrink-0" />
                      </button>

                      <Button
                        type="button"
                        size="icon"
                        variant="ghost"
                        onClick={(e) => {
                          e.stopPropagation();
                          setPendingDeleteId(s.sessionId);
                        }}
                        className="flex-shrink-0 text-destructive hover:text-destructive hover:bg-destructive/10"
                        aria-label="Delete session"
                      >
                        <Trash2 className="w-4 h-4" />
                      </Button>
                    </div>
                  </motion.div>
                );
              })}
            </motion.div>
          ) : (
            <motion.div
              key="albums"
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: 8 }}
              className="grid grid-cols-1 sm:grid-cols-2 gap-4"
            >
              {albums.map((a, i) => (
                <motion.button
                  key={a.category || "other"}
                  type="button"
                  initial={{ opacity: 0, y: 16 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: i * 0.04 }}
                  onClick={() => setExpandedCategory(a.category || "other")}
                  className="text-left glass rounded-2xl overflow-hidden shadow-glass hover:ring-1 hover:ring-primary/40 transition-all group"
                >
                  <div className="aspect-[4/3] overflow-hidden bg-muted">
                    <img
                      src={a.coverUrl}
                      alt={a.label}
                      className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500"
                    />
                  </div>
                  <div className="p-4">
                    <div className="flex items-center justify-between gap-2">
                      <h3 className="font-heading font-semibold text-foreground">{a.label}</h3>
                      <ChevronRight className="w-4 h-4 text-muted-foreground group-hover:text-primary transition-colors" />
                    </div>
                    <div className="flex items-center gap-3 mt-1 text-xs text-muted-foreground">
                      <span className="flex items-center gap-1">
                        <Images className="w-3 h-3" />
                        {a.totalPhotos} {a.totalPhotos === 1 ? "photo" : "photos"}
                      </span>
                      <span>·</span>
                      <span>
                        {a.sessions.length} {a.sessions.length === 1 ? "session" : "sessions"}
                      </span>
                    </div>
                  </div>
                </motion.button>
              ))}
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      <Dialog open={!!openSession} onOpenChange={(o) => !o && setOpenSession(null)}>
        <DialogContent className="glass max-w-3xl max-h-[90vh] overflow-y-auto">
          {openSession && (
            <>
              <DialogHeader>
                <DialogTitle className="flex items-center gap-2 text-foreground">
                  {categoryLabels[openSession.category || ""] || "Headshot"} session
                  <Badge variant="secondary" className="ml-1">
                    {openSession.jobs.length} {openSession.jobs.length === 1 ? "photo" : "photos"}
                  </Badge>
                </DialogTitle>
                <p className="text-sm text-muted-foreground">
                  {formatDate(openSession.createdAt)} · {formatTime(openSession.createdAt)}
                </p>
              </DialogHeader>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mt-2">
                {openSession.jobs.map((j, idx) => (
                  <div key={j.id} className="glass rounded-xl overflow-hidden shadow-glass">
                    <div className="aspect-square overflow-hidden bg-muted">
                      <img
                        src={j.output_image_url}
                        alt={`Variation ${idx + 1}`}
                        className="w-full h-full object-cover"
                        onError={() => handleImageError(j.id)}
                      />
                    </div>
                    <div className="p-2">
                      <Button
                        size="sm"
                        className="w-full gradient-cta text-primary-foreground btn-glow"
                        onClick={() => handleDownload(j.output_image_url, openSession.category, idx)}
                      >
                        <Download className="w-3 h-3 mr-1" />
                        Download
                      </Button>
                    </div>
                  </div>
                ))}
              </div>

              <div className="flex justify-end gap-2 pt-2">
                <Button
                  variant="outline"
                  className="glass border-border text-foreground"
                  onClick={() => handleRedo(openSession)}
                >
                  <RotateCcw className="w-4 h-4 mr-1" />
                  Regenerate
                </Button>
              </div>
            </>
          )}
        </DialogContent>
      </Dialog>

      <AlertDialog open={!!pendingDeleteId} onOpenChange={(o) => !o && !deleting && setPendingDeleteId(null)}>
        <AlertDialogContent className="glass">
          <AlertDialogHeader>
            <AlertDialogTitle>Delete this session?</AlertDialogTitle>
            <AlertDialogDescription>This cannot be undone.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleting}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={deleting}
              onClick={(e) => {
                e.preventDefault();
                if (pendingDeleteId) deleteSession(pendingDeleteId);
              }}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleting ? <Loader2 className="w-4 h-4 animate-spin" /> : "Delete"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </PageShell>
  );
};

export default History;
