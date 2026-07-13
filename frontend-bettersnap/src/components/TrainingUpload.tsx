import { useCallback, useRef, useState } from "react";
import { Loader2, Upload, X, AlertCircle, CheckCircle2 } from "lucide-react";
import {
  uploadPhoto,
  startTraining,
  TrainingRejectedError,
  type RejectedPhoto,
} from "@/lib/azureApi";
import { Button } from "@/components/ui/button";

/**
 * Multi-photo upload + "train my model".
 *
 * REPLACES the old single-photo flow, which read `e.target.files?.[0]` — so selecting 8
 * photos silently uploaded ONE and dropped the rest. Identity training needs the whole set.
 *
 * The server is the authority on which photos are usable: POST /train runs a face gate and
 * returns 400 with a per-photo `rejected` list. We surface that against the exact thumbnail
 * so the user knows WHICH photo to replace, instead of a generic "training failed" — a
 * faceless photo silently trained on would poison the model and only show up ~32 minutes
 * and one bad likeness later.
 */

const MIN_PHOTOS = 8;
const MAX_PHOTOS = 12;

interface Props {
  gender: string;
  onTrainingStarted: (info: { training_id: string; estimated_minutes: number }) => void;
  /** true when the user already has a model and this is a retrain. */
  isRetrain?: boolean;
  retrainCost?: number;
}

interface Item {
  file: File;
  preview: string;
  uploaded: boolean;
  rejected?: string;
}

export default function TrainingUpload({
  gender,
  onTrainingStarted,
  isRetrain = false,
  retrainCost = 0,
}: Props) {
  const [items, setItems] = useState<Item[]>([]);
  const [busy, setBusy] = useState(false);
  const [phase, setPhase] = useState("");
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const addFiles = useCallback((files: FileList | null) => {
    if (!files) return;
    setError(null);
    setItems((prev) => {
      const room = MAX_PHOTOS - prev.length;
      const incoming = Array.from(files)
        .filter((f) => /^image\/(jpeg|jpg|png|webp)$/i.test(f.type))
        .slice(0, Math.max(0, room))
        .map((f) => ({ file: f, preview: URL.createObjectURL(f), uploaded: false }));
      return [...prev, ...incoming];
    });
  }, []);

  const remove = (i: number) =>
    setItems((prev) => {
      URL.revokeObjectURL(prev[i].preview);
      return prev.filter((_, idx) => idx !== i);
    });

  const enough = items.length >= MIN_PHOTOS && items.length <= MAX_PHOTOS;

  const handleTrain = async () => {
    if (!enough || busy) return;
    setBusy(true);
    setError(null);
    setItems((prev) => prev.map((it) => ({ ...it, rejected: undefined })));

    try {
      // 1) Upload every photo. They land under THIS user's id server-side
      //    (inputs/<user_id>/input/<filename>) — exactly the prefix /train reads.
      //    Collect the blob_names: they are THIS session's set, and we send them to /train
      //    explicitly. /upload only ever APPENDS, so the folder can still contain a previous
      //    upload; letting the server just list the folder would train on both sets mixed
      //    together (in the worst case, two different people blended into one model).
      const uploaded: string[] = [];
      for (let i = 0; i < items.length; i++) {
        setPhase(`Uploading photo ${i + 1} of ${items.length}…`);
        const { blob_name } = await uploadPhoto(items[i].file);
        uploaded.push(blob_name);
        setItems((prev) => prev.map((it, idx) => (idx === i ? { ...it, uploaded: true } : it)));
      }

      // 2) Train on EXACTLY those photos. Crops, file list and prompts are computed server-side.
      setPhase("Checking your photos…");
      const res = await startTraining(gender, uploaded, isRetrain);
      onTrainingStarted({
        training_id: res.training_id,
        estimated_minutes: res.estimated_minutes,
      });
    } catch (e: any) {
      if (e instanceof TrainingRejectedError) {
        // Mark the exact photos the face gate refused, by index (1-based from the server).
        const byIndex = new Map<number, RejectedPhoto>(e.rejected.map((r) => [r.index, r]));
        setItems((prev) =>
          prev.map((it, idx) => {
            const r = byIndex.get(idx + 1);
            return r ? { ...it, rejected: r.reason } : it;
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

  return (
    <div className="space-y-5">
      <div>
        <h3 className="text-lg font-semibold">
          Upload {MIN_PHOTOS}–{MAX_PHOTOS} photos of yourself
        </h3>
        <p className="mt-1 text-sm text-muted-foreground">
          Different angles, expressions and lighting. Clear, front-facing shots of just you —
          no sunglasses, no group photos. We build a private model from these.
        </p>
      </div>

      <div className="grid grid-cols-3 gap-3 sm:grid-cols-4">
        {items.map((it, i) => (
          <div
            key={i}
            className={[
              "group relative aspect-square overflow-hidden rounded-lg border",
              it.rejected ? "border-destructive ring-2 ring-destructive/40" : "border-border",
            ].join(" ")}
          >
            <img src={it.preview} alt="" className="h-full w-full object-cover" />
            {it.uploaded && !it.rejected && (
              <CheckCircle2 className="absolute left-1 top-1 h-4 w-4 text-green-500" />
            )}
            {it.rejected && (
              <div className="absolute inset-x-0 bottom-0 bg-destructive/90 px-1 py-0.5 text-[10px] leading-tight text-white">
                No face detected — replace
              </div>
            )}
            {!busy && (
              <button
                type="button"
                onClick={() => remove(i)}
                className="absolute right-1 top-1 rounded-full bg-black/60 p-1 opacity-0 transition group-hover:opacity-100"
                aria-label="Remove photo"
              >
                <X className="h-3 w-3 text-white" />
              </button>
            )}
          </div>
        ))}

        {items.length < MAX_PHOTOS && !busy && (
          <button
            type="button"
            onClick={() => inputRef.current?.click()}
            className="flex aspect-square flex-col items-center justify-center gap-1 rounded-lg border-2 border-dashed border-border text-muted-foreground transition hover:border-primary hover:text-primary"
          >
            <Upload className="h-5 w-5" />
            <span className="text-xs">Add</span>
          </button>
        )}
      </div>

      <input
        ref={inputRef}
        type="file"
        accept="image/jpeg,image/jpg,image/png,image/webp"
        multiple
        hidden
        onChange={(e) => {
          addFiles(e.target.files);
          e.target.value = ""; // let the same file be re-picked after a removal
        }}
      />

      <p className="text-sm text-muted-foreground">
        {items.length} of {MIN_PHOTOS}–{MAX_PHOTOS} selected
        {items.length > 0 && items.length < MIN_PHOTOS && (
          <span className="text-foreground"> · add {MIN_PHOTOS - items.length} more</span>
        )}
      </p>

      {error && (
        <div className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {isRetrain && retrainCost > 0 && (
        <p className="text-sm text-muted-foreground">
          Retraining costs <strong>{retrainCost} credits</strong>.
        </p>
      )}

      <Button onClick={handleTrain} disabled={!enough || busy} className="w-full" size="lg">
        {busy ? (
          <>
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            {phase || "Working…"}
          </>
        ) : isRetrain ? (
          "Retrain my model"
        ) : (
          "Create my model"
        )}
      </Button>
    </div>
  );
}
