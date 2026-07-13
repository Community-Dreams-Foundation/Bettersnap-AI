import { useEffect, useRef, useState } from "react";
import { AlertCircle, CheckCircle2, Loader2 } from "lucide-react";
import { getTrainingStatus, type TrainingStatus } from "@/lib/azureApi";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";

/**
 * "We're building your model" screen.
 *
 * Training takes ~32 minutes (warm class-image cache; the very first user of a given
 * gender also pays a one-off ~17 min to build it). That is far too long for a spinner, and
 * far too long to hold in memory — so:
 *
 *  - Progress is derived from the training row's created_at, NOT from a timer in component
 *    state. The user can refresh, close the tab, come back on their phone, and still see a
 *    truthful bar. A client-side timer would reset to 0% and look broken.
 *  - Polling is server-truth (GET /train/status) every 15s, not an optimistic animation.
 *  - The bar is capped below 100% until the server actually says `ready`, so we never show
 *    a finished bar for an unfinished model.
 */

const ESTIMATED_MINUTES = 32;

interface Props {
  onReady: () => void;
  onFailed?: (error: string | null) => void;
}

export default function TrainingProgress({ onReady, onFailed }: Props) {
  const [status, setStatus] = useState<TrainingStatus | null>(null);
  const [elapsedMin, setElapsedMin] = useState(0);
  const [pollError, setPollError] = useState<string | null>(null);
  const done = useRef(false);

  useEffect(() => {
    let alive = true;

    const tick = async () => {
      try {
        const s = await getTrainingStatus();
        if (!alive) return;
        setStatus(s);
        setPollError(null);

        // Elapsed comes from the SERVER's created_at, so a refresh doesn't reset it.
        const started = s.training?.created_at ? Date.parse(s.training.created_at) : NaN;
        if (!Number.isNaN(started)) {
          setElapsedMin(Math.max(0, (Date.now() - started) / 60000));
        }

        if (!done.current && s.lora_status === "ready") {
          done.current = true;
          onReady();
        } else if (!done.current && s.lora_status === "failed") {
          done.current = true;
          onFailed?.(s.training?.error ?? null);
        }
      } catch (e: any) {
        // A transient network blip must not look like a failed model.
        if (alive) setPollError(e?.message ?? "Connection problem");
      }
    };

    tick();
    const id = setInterval(tick, 15000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [onReady, onFailed]);

  const failed = status?.lora_status === "failed";
  const ready = status?.lora_status === "ready";

  // Cap at 95% until the server confirms — never show a full bar for an unfinished model.
  const pct = ready ? 100 : Math.min(95, Math.round((elapsedMin / ESTIMATED_MINUTES) * 100));
  const remaining = Math.max(1, Math.ceil(ESTIMATED_MINUTES - elapsedMin));

  if (failed) {
    return (
      <div className="space-y-4 text-center">
        <AlertCircle className="mx-auto h-10 w-10 text-destructive" />
        <div>
          <h3 className="text-lg font-semibold">We couldn't build your model</h3>
          <p className="mt-1 text-sm text-muted-foreground">
            {status?.training?.error ?? "Something went wrong during training."}
          </p>
        </div>
        <Button onClick={() => window.location.reload()}>Try again</Button>
      </div>
    );
  }

  if (ready) {
    return (
      <div className="space-y-3 text-center">
        <CheckCircle2 className="mx-auto h-10 w-10 text-green-500" />
        <h3 className="text-lg font-semibold">Your model is ready</h3>
      </div>
    );
  }

  return (
    <div className="space-y-5 text-center">
      <Loader2 className="mx-auto h-10 w-10 animate-spin text-primary" />
      <div>
        <h3 className="text-lg font-semibold">Building your personal model</h3>
        <p className="mt-1 text-sm text-muted-foreground">
          This takes about {ESTIMATED_MINUTES} minutes. You can close this page — we'll keep
          going, and any photos you've requested will start automatically when it's done.
        </p>
      </div>

      <div className="space-y-2">
        <Progress value={pct} />
        <p className="text-xs text-muted-foreground">
          {pct}% · about {remaining} minute{remaining === 1 ? "" : "s"} left
        </p>
      </div>

      {pollError && (
        <p className="text-xs text-muted-foreground">
          Reconnecting… ({pollError})
        </p>
      )}
    </div>
  );
}
