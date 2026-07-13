-- Add session_id to group multiple headshot variations from a single batch
ALTER TABLE public.headshot_jobs
ADD COLUMN IF NOT EXISTS session_id uuid;

-- Backfill: each existing job becomes its own single-image session
UPDATE public.headshot_jobs
SET session_id = id
WHERE session_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_headshot_jobs_session_id ON public.headshot_jobs(session_id);
CREATE INDEX IF NOT EXISTS idx_headshot_jobs_user_created ON public.headshot_jobs(user_id, created_at DESC);