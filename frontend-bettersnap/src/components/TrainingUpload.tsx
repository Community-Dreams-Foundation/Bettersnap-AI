import { useCallback, useRef, useState, type DragEvent } from "react";
import { Loader2, Upload, X, AlertCircle, CheckCircle2, ImagePlus } from "lucide-react";
import { uploadPhoto, startTraining, TrainingRejectedError, type RejectedPhoto } from "@/lib/azureApi";
import { Button } from "@/components/ui/button";

/**
 * Multi-photo upload + "train my model".
 *
 * The server is the authority on which photos are usable: POST /train runs a face gate and
 * returns 400 with a per-photo `rejected` list. We surface that against the exact thumbnail
 * so the user knows WHICH photo to replace, instead of a generic "training failed".
 */

const MIN_PHOTOS = 8;
const MAX_PHOTOS = 12;

// Friendly text per backend rejection code. Falls back to the raw reason if a new
// code shows up that isn't mapped here yet.
const CODE_MESSAGES: Record<string, string> = {
  FACE_NOT_FOUND: "No clear face — replace",
  MULTIPLE_FACES: "More than one face — replace",
  NOT_AN_IMAGE: "Unreadable image — replace",
};

interface Props {
  gender: string;
  onTrainingStarted: (info: { training_id: string; estimated_minutes: number }) => void;
  isRetrain?: boolean;
  retrainCost?: number;
}

type ItemStatus = "pending" | "uploading" | "accepted" | "rejected";

interface Item {
  file: File;
  preview: string;
  status: ItemStatus;
  rejected?: string;
  code?: string;
}

export default function TrainingUpload({ gender, onTrainingStarted, isRetrain = false, retrainCost = 0 }: Props) {
  const [items, setItems] = useState<Item[]>([]);
  const [busy, setBusy] = useState(false);
  const [phase, setPhase] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const addFiles = useCallback((files: FileList | File[] | null) => {
    if (!files) return;
    setError(null);
    setItems((prev) => {
      const room = MAX_PHOTOS - prev.length;
      const incoming = Array.from(files)
        .filter((f) => /^image\/(jpeg|jpg|png|webp)$/i.test(f.type))
        .slice(0, Math.max(0, room))
        .map<Item>((f) => ({ file: f, preview: URL.createObjectURL(f), status: "pending" }));
      return [...prev, ...incoming];
    });
  }, []);

  const remove = (i: number) =>
    setItems((prev) => {
      URL.revokeObjectURL(prev[i].preview);
      return prev.filter((_, idx) => idx !== i);
    });

  const enough = items.length >= MIN_PHOTOS && items.length <= MAX_PHOTOS;

  const handleDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragOver(false);
    if (busy) return;
    addFiles(e.dataTransfer.files);
  };

  const handleTrain = async () => {
    if (!enough || busy) return;
    setBusy(true);
    setError(null);
    setItems((prev) => prev.map((it) => ({ ...it, rejected: undefined, status: "pending" as ItemStatus })));

    try {
      const uploaded: string[] = [];
      for (let i = 0; i < items.length; i++) {
        setPhase(`Uploading photo ${i + 1} of ${items.length}…`);
        setItems((prev) => prev.map((it, idx) => (idx === i ? { ...it, status: "uploading" } : it)));
        const { blob_name } = await uploadPhoto(items[i].file);
        uploaded.push(blob_name);
        setItems((prev) => prev.map((it, idx) => (idx === i ? { ...it, status: "accepted" } : it)));
      }

      setPhase("Checking your photos…");
      const res = await startTraining(gender, uploaded, isRetrain);
      onTrainingStarted({
        training_id: res.training_id,
        estimated_minutes: res.estimated_minutes,
      });
    } catch (e: any) {
      if (e instanceof TrainingRejectedError) {
        const byIndex = new Map<number, RejectedPhoto>(e.rejected.map((r) => [r.index, r]));
        setItems((prev) =>
          prev.map((it, idx) => {
            const r = byIndex.get(idx + 1);
            return r ? { ...it, status: "rejected" as ItemStatus, rejected: r.reason, code: r.code } : it;
          }),
        );
        setError(e.message);
      } else {
        setError(e?.message ?? "Could not start training");
      }
    } finally {
      setBusy(false);
      setPhase("");
    }
  };

  const validCount = items.filter((i) => i.status !== "rejected").length;

  return (
    <div className="space-y-6">
      {/* Dropzone */}
      <div
        onDragOver={(e) => {
          e.preventDefault();
          if (!busy) setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
        onClick={() => !busy && inputRef.current?.click()}
        className={[
          "relative flex flex-col items-center justify-center gap-3 rounded-2xl border-2 border-dashed p-8 text-center transition-all cursor-pointer",
          dragOver
            ? "border-primary bg-primary/5"
            : "border-border bg-secondary/40 hover:border-primary/50 hover:bg-primary/[0.03]",
          busy ? "pointer-events-none opacity-60" : "",
        ].join(" ")}
        role="button"
        tabIndex={0}
      >
        <div className="w-12 h-12 rounded-2xl bg-primary/10 flex items-center justify-center">
          <ImagePlus className="w-6 h-6 text-primary" />
        </div>
        <div>
          <p className="text-sm font-semibold text-foreground">
            Drag & drop your photos here
          </p>
          <p className="text-xs text-muted-foreground mt-1">
            or click to browse · JPG, PNG, WEBP · {MIN_PHOTOS}–{MAX_PHOTOS} photos
          </p>
        </div>
      </div>

      <input
        ref={inputRef}
        type="file"
        accept="image/jpeg,image/jpg,image/png,image/webp"
        multiple
        hidden
        onChange={(e) => {
          addFiles(e.target.files);
          e.target.value = "";
        }}
      />

      {/* Grid */}
      {items.length > 0 && (
        <div>
          <div className="flex items-center justify-between mb-3">
            <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Your photos
            </p>
            <p className="text-xs text-muted-foreground">
              <span className="font-semibold text-foreground">{validCount}</span> of {MIN_PHOTOS}–{MAX_PHOTOS}
            </p>
          </div>
          <div className="grid grid-cols-3 gap-3 sm:grid-cols-4">
            {items.map((it, i) => (
              <div
                key={i}
                className={[
                  "group relative aspect-square overflow-hidden rounded-xl border bg-card shadow-sm",
                  it.status === "rejected"
                    ? "border-destructive ring-2 ring-destructive/30"
                    : it.status === "accepted"
                    ? "border-primary/40"
                    : "border-border",
                ].join(" ")}
              >
                <img src={it.preview} alt="" className="h-full w-full object-cover" />

                {it.status === "uploading" && (
                  <div className="absolute inset-0 bg-background/70 backdrop-blur-sm flex items-center justify-center">
                    <Loader2 className="w-5 h-5 animate-spin text-primary" />
                  </div>
                )}

                {it.status === "accepted" && (
                  <div className="absolute left-1.5 top-1.5 w-5 h-5 rounded-full bg-primary flex items-center justify-center shadow">
                    <CheckCircle2 className="w-3.5 h-3.5 text-primary-foreground" />
                  </div>
                )}

                {it.status === "rejected" && (
                  <div className="absolute inset-x-0 bottom-0 bg-destructive/95 px-1.5 py-1 text-[10px] leading-tight text-destructive-foreground">
                    {(it.code && CODE_MESSAGES[it.code]) || it.rejected || "No face detected"}
                  </div>
                )}

                {!busy && (
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      remove(i);
                    }}
                    className="absolute right-1.5 top-1.5 rounded-full bg-foreground/70 p-1 opacity-0 transition group-hover:opacity-100 hover:bg-foreground"
                    aria-label="Remove photo"
                  >
                    <X className="h-3 w-3 text-background" />
                  </button>
                )}
              </div>
            ))}

            {items.length < MAX_PHOTOS && !busy && (
              <button
                type="button"
                onClick={() => inputRef.current?.click()}
                className="flex aspect-square flex-col items-center justify-center gap-1 rounded-xl border-2 border-dashed border-border text-muted-foreground transition hover:border-primary hover:text-primary hover:bg-primary/5"
              >
                <Upload className="h-5 w-5" />
                <span className="text-xs font-medium">Add more</span>
              </button>
            )}
          </div>
        </div>
      )}

      {items.length > 0 && items.length < MIN_PHOTOS && (
        <p className="text-xs text-muted-foreground">
          Add <span className="font-semibold text-foreground">{MIN_PHOTOS - items.length}</span> more to start training.
        </p>
      )}

      {error && (
        <div className="flex items-start gap-2 rounded-xl border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {isRetrain && retrainCost > 0 && (
        <p className="text-sm text-muted-foreground">
          Retraining costs <strong>{retrainCost} credits</strong>.
        </p>
      )}

      <Button
        onClick={handleTrain}
        disabled={!enough || busy}
        className="w-full gradient-cta text-primary-foreground font-semibold btn-glow disabled:opacity-50"
        size="lg"
      >
        {busy ? (
          <>
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            {phase || "Working…"}
          </>
        ) : isRetrain ? (
          "Retrain my model"
        ) : (
          "Start AI Training"
        )}
      </Button>
    </div>
  );
}
