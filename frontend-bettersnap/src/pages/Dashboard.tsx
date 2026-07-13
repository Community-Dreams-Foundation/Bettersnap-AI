import { useState, useEffect } from "react";
import Navbar from "@/components/Navbar";
import PageShell from "@/components/PageShell";
import ComplianceChecker from "@/components/ComplianceChecker";
import { motion } from "framer-motion";
import { Download, RefreshCw, Loader2, ImageOff, BarChart3, Image as ImageIcon, Zap, ShieldCheck } from "lucide-react";
import { Link } from "react-router-dom";
import { BASE_URL, authHeaders, getUserJobs, getJobResultUrls, type AzureJob } from "@/lib/azureApi";
import { downloadImage } from "@/lib/download";
import { useAuth } from "@/contexts/AuthContext";

const formats = [
  {
    id: "headshot1",
    label: "Professional Headshot 1",
    summary: "Professional headshot with navy blue suit",
    useCase: "LinkedIn",
    width: 800,
    height: 800,
  },
  {
    id: "headshot2",
    label: "Professional Headshot 2",
    summary: "Professional headshot with navy blue suit",
    useCase: "LinkedIn",
    width: 800,
    height: 800,
  },
  {
    id: "headshot3",
    label: "Professional Headshot 3",
    summary: "Professional headshot with navy blue suit",
    useCase: "LinkedIn",
    width: 800,
    height: 800,
  },
  {
    id: "headshot4",
    label: "Professional Headshot 4",
    summary: "Professional headshot with navy blue suit",
    useCase: "LinkedIn",
    width: 800,
    height: 800,
  },
];

async function downloadResized(imageUrl: string, width: number, height: number, filename: string) {
  try {
    const response = await fetch(imageUrl);
    const blob = await response.blob();
    const bitmapUrl = URL.createObjectURL(blob);
    const img = new window.Image();
    img.onload = () => {
      const canvas = document.createElement("canvas");
      canvas.width = width;
      canvas.height = height;
      const ctx = canvas.getContext("2d");
      if (!ctx) {
        URL.revokeObjectURL(bitmapUrl);
        return;
      }
      ctx.drawImage(img, 0, 0, width, height);
      canvas.toBlob(
        (canvasBlob) => {
          if (!canvasBlob) return;
          const url = URL.createObjectURL(canvasBlob);
          const a = document.createElement("a");
          a.href = url;
          a.download = filename;
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          URL.revokeObjectURL(url);
        },
        "image/jpeg",
        0.95,
      );
      URL.revokeObjectURL(bitmapUrl);
    };
    img.src = bitmapUrl;
  } catch {
    await downloadImage(imageUrl, filename);
  }
}

const Dashboard = () => {
  const { user } = useAuth();
  const [selectedFormat, setSelectedFormat] = useState(formats[0]);
  const [regenerating, setRegenrating] = useState<string | null>(null);
  const [outputUrls, setOutputUrls] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [photoCount, setPhotoCount] = useState<number>(0);
  const [credits, setCredits] = useState<number>(0);

  const displayName = user?.name || user?.username || "there";


  useEffect(() => {
    if (!user) return;

    const fetchDashboardData = async () => {
      try {
        // Credits. This used to GET /users/profile — a route that DOES NOT EXIST (404) —
        // and then read `profile.credits`, when the API returns `credits_remaining`. Two
        // bugs stacked: the call always threw, and even on success it would have read
        // undefined. The dashboard therefore showed "0 credits" to everyone, always.
        const headers = await authHeaders();
        const profileRes = await fetch(`${BASE_URL}/profiles/me`, { method: "GET", headers });
        if (profileRes.ok) {
          const profile = await profileRes.json();
          setCredits(profile?.credits_remaining ?? 0);
        }

        const jobs = await getUserJobs();

        const completedJobs = jobs.filter((job) => job.status === "completed");

        // "Photos Generated" means PHOTOS, not jobs. This counted completed JOBS, so a user
        // who generated 10 images in one job saw "1". Sum the actual images instead —
        // output_blob_path is the list of rendered images on the job row.
        setPhotoCount(
          completedJobs.reduce(
            (n, j) => n + (Array.isArray(j.output_blob_path) ? j.output_blob_path.length : 0),
            0,
          ),
        );
        if (completedJobs.length === 0) return;

        const latestJob = completedJobs.sort(
          (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
        )[0];

        // Get image URLs for latest job
        const urls = await getJobResultUrls(latestJob.job_id);
        setOutputUrls(urls);
      } catch (err) {
        console.error("Failed to load dashboard data:", err);
      } finally {
        setLoading(false);
      }
    };


    fetchDashboardData();
  }, [user]);

  const stats = [
    { icon: ImageIcon, label: "Photos Generated", value: photoCount > 0 ? String(photoCount) : "—" },
    { icon: BarChart3, label: "Formats Available", value: "4" },
    { icon: Zap, label: "Avg. Speed", value: "<2 min" },
  ];

  const handleDownload = (index: number) => {
    const url = outputUrls[index];
    if (!url) return;
    const filename = `bettersnap-headshot-${index + 1}.jpg`;
    downloadResized(url, formats[index].width, formats[index].height, filename);
  };

  const handleRegenerate = (id: string) => {
    setRegenrating(id);
    setTimeout(() => setRegenrating(null), 2000);
  };

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
          <h1 className="text-3xl font-heading font-bold text-foreground mb-2">Recent Headshots</h1>
          <p className="text-muted-foreground">All your generated headshots in every format, ready to download.</p>
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

        {loading ? (
          <div className="flex items-center justify-center py-20" role="status" aria-label="Loading headshots">
            <Loader2 className="w-8 h-8 text-primary animate-spin" />
          </div>
        ) : outputUrls.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-20 text-center glass rounded-2xl shadow-glass">
            <ImageOff className="w-12 h-12 text-muted-foreground mb-4" aria-hidden="true" />
            <p className="text-foreground font-medium">No headshots yet</p>
            <p className="text-sm text-muted-foreground mt-1">
              Generate a headshot first, then come back here to download in all formats.
            </p>
          </div>
        ) : (
          <div className="grid lg:grid-cols-3 gap-6">
            <div className="lg:col-span-2">
              <div className="grid sm:grid-cols-2 gap-4">
                {formats.map((fmt, i) => (
                  <motion.div
                    key={fmt.id}
                    initial={{ opacity: 0, y: 20 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: i * 0.08 }}
                    className={`glass rounded-2xl overflow-hidden cursor-pointer hover-scale transition-all ${
                      selectedFormat.id === fmt.id
                        ? "ring-2 ring-primary glow-primary"
                        : "hover:ring-1 hover:ring-primary/30"
                    }`}
                    onClick={() => setSelectedFormat(fmt)}
                    role="button"
                    tabIndex={0}
                    aria-label={`Select ${fmt.label}`}
                    aria-pressed={selectedFormat.id === fmt.id}
                    onKeyDown={(e) => e.key === "Enter" && setSelectedFormat(fmt)}
                  >
                    <div className="relative">
                      {regenerating === fmt.id ? (
                        <div className="w-full aspect-square shimmer" />
                      ) : (
                        <img
                          src={outputUrls[i] ?? outputUrls[0]}
                          alt={fmt.label}
                          className="w-full aspect-square object-cover"
                          onError={(e) => {
                            (e.target as HTMLImageElement).style.display = "none";
                          }}
                        />
                      )}
                    </div>
                    <div className="p-4">
                      <p className="text-sm font-semibold text-foreground">{fmt.label}</p>
                      <p className="text-xs text-muted-foreground mt-0.5">{fmt.summary}</p>
                      <div className="flex gap-2 mt-3">
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            handleDownload(i);
                          }}
                          className="flex items-center gap-1 text-xs px-3 py-1.5 rounded-lg gradient-cta text-primary-foreground font-medium hover-scale btn-glow"
                          aria-label={`Download ${fmt.label}`}
                        >
                          <Download className="w-3 h-3" aria-hidden="true" /> Download
                        </button>
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            handleRegenerate(fmt.id);
                          }}
                          className="flex items-center gap-1 text-xs px-3 py-1.5 rounded-lg glass text-muted-foreground hover:text-foreground"
                          aria-label={`Regenerate ${fmt.label}`}
                        >
                          <RefreshCw className="w-3 h-3" aria-hidden="true" /> Redo
                        </button>
                      </div>
                    </div>
                  </motion.div>
                ))}
              </div>
            </div>

            <div className="space-y-4">
              <div className="glass rounded-2xl p-6 shadow-glass">
                <div className="w-10 h-10 rounded-full bg-primary/10 flex items-center justify-center mb-3">
                  <ShieldCheck className="w-5 h-5 text-primary" aria-hidden="true" />
                </div>
                <h3 className="font-heading font-semibold text-foreground mb-2">Your photos stay private</h3>
                <p className="text-sm text-muted-foreground">
                  Uploaded photos are used only to generate your selected headshots. We do not display your images publicly.
                </p>
              </div>
              <ComplianceChecker useCase={selectedFormat.useCase} />
              <div className="glass rounded-2xl p-6 shadow-glass">
                <h3 className="font-heading font-semibold text-foreground mb-2">Quick Tips</h3>
                <ul className="space-y-2 text-sm text-muted-foreground">
                  <li className="flex items-start gap-2">
                    <span className="text-primary mt-0.5" aria-hidden="true">
                      •
                    </span>
                    LinkedIn photos should show your face clearly
                  </li>
                  <li className="flex items-start gap-2">
                    <span className="text-primary mt-0.5" aria-hidden="true">
                      •
                    </span>
                    White backgrounds work great for professional photos
                  </li>
                </ul>
              </div>
            </div>
          </div>
        )}
      </div>
    </PageShell>
  );
};

export default Dashboard;
