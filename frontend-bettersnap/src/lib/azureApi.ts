import { msalInstance } from "../main";
import { tokenRequest } from "./authConfig";

export const BASE_URL = import.meta.env.VITE_AZURE_API_URL;

// Acquire the Entra (MSAL) access token from the shared msalInstance — the same
// instance MsalProvider/AuthContext use, so it sees the signed-in account.
// This replaces the old Supabase 'sb-' localStorage read, which returned nothing
// under MSAL auth and made every backend call 401. Mirrors
// AuthContext.getAccessToken so the token sent here is identical to the rest of
// the app's Entra token (same account, same tokenRequest scopes).
async function getToken(): Promise<string> {
  const account = msalInstance.getActiveAccount() ?? msalInstance.getAllAccounts()[0];
  if (!account) throw new Error("No signed-in MSAL account");
  try {
    const response = await msalInstance.acquireTokenSilent({
      ...tokenRequest,
      account,
      // forceRefresh bypasses the MSAL access-token cache and hits the token
      // endpoint for a NEW token scoped to the API (via the cached refresh
      // token). Without it, acquireTokenSilent can return a cached token from
      // the login flow — aud=00000003-…(Microsoft Graph), scp="email openid
      // profile", ver 1.0 — which the backend rejects with "Signature
      // verification failed". Forcing a refresh guarantees aud=api://d14bccac-…
      forceRefresh: true,
    });
    return response.accessToken;
  } catch {
    // Silent acquisition failed (e.g. expired/again-consent) — fall back to an
    // interactive redirect, exactly as AuthContext.getAccessToken does. The
    // redirect navigates away, so this call cannot return a token.
    await msalInstance.acquireTokenRedirect(tokenRequest);
    throw new Error("MSAL interaction required; redirecting to sign in");
  }
}

export async function authHeaders(): Promise<Record<string, string>> {
  const token = await getToken();
  return {
    Authorization: `Bearer ${token}`,
    "Content-Type": "application/json",
  };
}

export async function registerUser() {
  const headers = await authHeaders();
  const res = await fetch(`${BASE_URL}/users/register`, { method: "POST", headers });
  if (!res.ok) throw new Error(`Register failed: ${res.status}`);
  return res.json();
}

export async function getUserCredits(): Promise<{ credits_remaining: number }> {
  const headers = await authHeaders();
  const res = await fetch(`${BASE_URL}/users/credits`, { method: "GET", headers });
  if (!res.ok) throw new Error(`Credits fetch failed: ${res.status}`);
  return res.json();
}

// "waiting_lora" = the job was ACCEPTED and its credits reserved, but the user's identity
// model is still training. The backend deliberately does NOT enqueue it; the training
// watcher releases it the instant the model is ready. It is a normal, healthy state — not
// an error — and the UI must not treat it as "stuck".
export type JobStatus =
  | "waiting_lora"
  | "queued"
  | "dispatching"
  | "processing"
  | "completed"
  | "failed";

export interface AzureJob {
  job_id: string;
  user_id: string;
  status: JobStatus;
  input_blob_path: string;
  job_params: Record<string, any>;
  output_blob_path: string[];
  job_type: string;
  category: string;
  created_at: string;
  completed_at: string | null;
}

// ── Identity-LoRA training ───────────────────────────────────────────────────
export type LoraStatus = "none" | "training" | "ready" | "failed";

export interface TrainingStatus {
  lora_status: LoraStatus;
  training?: {
    training_id: string;
    status: "queued" | "dispatching" | "training" | "completed" | "failed";
    photos: number;
    error: string | null;
    created_at: string | null;
    completed_at: string | null;
  };
}

export interface StartTrainingResult {
  training_id: string;
  status: "training";
  photos: number;
  class_word: string;
  retrain: boolean;
  credits_charged: number;
  estimated_minutes: number;
}

/** Rejected photo from the server-side FACE GATE (HTTP 400 from POST /train). */
export interface RejectedPhoto {
  photo: string;
  index: number;
  reason: string;
}

export class TrainingRejectedError extends Error {
  rejected: RejectedPhoto[];
  constructor(message: string, rejected: RejectedPhoto[]) {
    super(message);
    this.name = "TrainingRejectedError";
    this.rejected = rejected;
  }
}

/**
 * Kick off this user's identity-LoRA training. Everything (crops, file list, prompts) is
 * computed server-side from the photos already uploaded under their user id — the client
 * sends only gender.
 *
 * Throws TrainingRejectedError when a photo fails the face gate, so the caller can point
 * at the specific photo instead of showing a generic failure.
 */
export async function startTraining(
  gender: string,
  photos: string[],
  force = false,
): Promise<StartTrainingResult> {
  const headers = await authHeaders();
  // `photos` = the blob_names just returned by /upload, i.e. THIS session's set.
  // Without it the server falls back to listing the whole folder — and since /upload only
  // ever appends, that folder can still hold a previous upload. Training would then blend
  // two different photo sets (in the worst case, two different people) into one model.
  const res = await fetch(`${BASE_URL}/train`, {
    method: "POST",
    headers,
    body: JSON.stringify({ gender, force, photos }),
  });
  if (!res.ok) {
    let body: any = {};
    try {
      body = await res.json();
    } catch {
      /* non-JSON body */
    }
    if (res.status === 400 && Array.isArray(body?.rejected)) {
      throw new TrainingRejectedError(body.error ?? "Some photos were rejected", body.rejected);
    }
    throw new Error(body?.error ?? body?.message ?? `Training failed: ${res.status}`);
  }
  return res.json();
}

export async function getTrainingStatus(): Promise<TrainingStatus> {
  const headers = await authHeaders();
  const res = await fetch(`${BASE_URL}/train/status`, { method: "GET", headers });
  if (!res.ok) throw new Error(`Training status failed: ${res.status}`);
  return res.json();
}

/**
 * Poll until the model is ready (or failed). Training takes ~32 minutes on a warm class
 * cache, so this deliberately has NO short timeout — a spinner that gives up after 10
 * minutes would abandon a run that is progressing perfectly well.
 */
export async function pollTrainingUntilDone(
  onStatus?: (s: TrainingStatus) => void,
  intervalMs = 15000,
  maxMinutes = 90,
): Promise<LoraStatus> {
  const deadline = Date.now() + maxMinutes * 60_000;
  for (;;) {
    const s = await getTrainingStatus();
    onStatus?.(s);
    if (s.lora_status === "ready" || s.lora_status === "failed") return s.lora_status;
    if (Date.now() > deadline) throw new Error("Training timed out");
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

export async function getUserJobs(): Promise<AzureJob[]> {
  const headers = await authHeaders();
  const res = await fetch(`${BASE_URL}/users/jobs`, { method: "GET", headers });
  if (!res.ok) throw new Error(`Jobs fetch failed: ${res.status}`);
  // The API returns { jobs: [...] }, not a bare array. This used to `return res.json()`
  // while claiming to return AzureJob[], so callers got an OBJECT and every
  // `jobs.filter(...)` threw "filter is not a function". Dashboard and History caught it
  // and logged to console, so both pages just silently rendered empty — they have never
  // actually shown a job.
  const data = await res.json();
  return (Array.isArray(data) ? data : (data?.jobs ?? [])) as AzureJob[];
}

export async function deleteJob(jobId: string): Promise<void> {
  const headers = await authHeaders();
  const res = await fetch(`${BASE_URL}/jobs/${jobId}`, {
    method: "DELETE",
    headers,
  });
  if (!res.ok) throw new Error(`Delete failed: ${res.status}`);
}

export async function getJobResultUrls(jobId: string): Promise<string[]> {
  const headers = await authHeaders();
  const res = await fetch(`${BASE_URL}/jobs/${jobId}/result-url`, {
    method: "GET",
    headers,
  });
  if (!res.ok) throw new Error(`Result URL fetch failed: ${res.status}`);
  const data = await res.json();
  return data.urls || (data.url ? [data.url] : []);
}

export async function uploadPhoto(file: File): Promise<{ url: string; blob_name: string }> {
  const token = await getToken();
  const formData = new FormData();
  formData.append("photo", file);
  const res = await fetch(`${BASE_URL}/upload`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    body: formData,
  });
  if (!res.ok) throw new Error(`Upload failed: ${res.status}`);
  return res.json();
}

// ── Catalog + Plans (backend is the single source of truth) ─────────────────
// The picker options and plan limits come from the SAME modules the backend
// enforces against (shared/catalog.py, shared/plans.py), so the UI can never
// drift from server-side validation. Fetch these once and cache in the page.
export interface CatalogOption {
  id: string;
  ref: string;   // category-qualified "category.option" — send THIS in attire_ids/background_ids
  label: string;
}
export interface CatalogCategory {
  id: string;
  type: "professional" | "personal";
  label: string;
  custom: boolean;
  attires: CatalogOption[];
  backgrounds: CatalogOption[];
}
export interface Plan {
  key: string;
  name: string;
  price_usd: number;
  image_count: number;
  max_attires: number;
  max_backgrounds: number;
  category_rule: "single_type" | "mixable";
  plan_type: "one_time" | "monthly";
  credits_per_image: number;
  min_session_images: number;
  monthly_images: number;
}

export async function getCatalog(): Promise<CatalogCategory[]> {
  const res = await fetch(`${BASE_URL}/catalog`, { method: "GET" });
  if (!res.ok) throw new Error(`Catalog fetch failed: ${res.status}`);
  const data = await res.json();
  return (data?.categories ?? []) as CatalogCategory[];
}

export async function getPlans(): Promise<Plan[]> {
  const res = await fetch(`${BASE_URL}/plans`, { method: "GET" });
  if (!res.ok) throw new Error(`Plans fetch failed: ${res.status}`);
  const data = await res.json();
  return (data?.plans ?? []) as Plan[];
}

// Submit a generation. Selections are GLOBAL cross-category refs from the catalog
// (e.g. "business_suit.navy_suit_tie"). Provide attire_ids + background_ids for a
// menu generation, OR custom_prompt for the custom_scene mode (scene-only text).
// image_count is NOT sent — the backend derives it from the user's plan.
/** The user has no trained model, so generation is impossible — send them to training. */
export class ModelNotTrainedError extends Error {
  loraStatus: LoraStatus;
  constructor(message: string, loraStatus: LoraStatus) {
    super(message);
    this.name = "ModelNotTrainedError";
    this.loraStatus = loraStatus;
  }
}

// input_blob_path is NOT sent: this is txt2img, identity comes from the trained LoRA, and
// there is no source photo. The backend no longer requires it.
export async function submitJob(params: {
  gender: string;
  age_range: string;
  hair_color: string;
  attire_ids: string[];
  background_ids: string[];
  custom_prompt?: string;
}): Promise<{ job_id: string; status: "queued" | "waiting_lora"; message?: string }> {
  const headers = await authHeaders();
  const res = await fetch(`${BASE_URL}/jobs/submit`, {
    method: "POST",
    headers,
    body: JSON.stringify({ custom_prompt: "", ...params }),
  });
  if (!res.ok) {
    let err: any = {};
    try {
      err = await res.json();
    } catch {
      /* non-JSON body */
    }
    // 409 = no trained model yet. This is a ROUTING signal, not a failure to show as an
    // error toast — the user needs to go and train.
    if (res.status === 409) {
      throw new ModelNotTrainedError(
        err?.error ?? "Your model isn't trained yet.",
        (err?.lora_status ?? "none") as LoraStatus,
      );
    }
    // Structured plan/validation errors (limits, type-mix, credits, daily caps).
    throw new Error(err?.error ? String(err.error) : `Submit failed: ${res.status}`);
  }
  return res.json();
}

export async function getJobStatus(jobId: string): Promise<{
  status: JobStatus;
  output_blob_path: string | null;
}> {
  const headers = await authHeaders();
  const res = await fetch(`${BASE_URL}/jobs/${jobId}/status`, { method: "GET", headers });
  if (!res.ok) throw new Error(`Status fetch failed: ${res.status}`);
  return res.json();
}

export async function getJobResultUrl(jobId: string): Promise<{ url: string }> {
  const headers = await authHeaders();
  const res = await fetch(`${BASE_URL}/jobs/${jobId}/result-url`, { method: "GET", headers });
  if (!res.ok) throw new Error(`Result URL fetch failed: ${res.status}`);
  return res.json();
}

/**
 * Poll a generation job to completion and return its image URLs.
 *
 * Two things the old version got wrong:
 *  - It gave up after 10 minutes. A job parked as `waiting_lora` waits for a ~32-minute
 *    training run to finish, so a 10-minute cap abandoned jobs that were perfectly healthy.
 *    The budget is now split: an unlimited-ish wait while `waiting_lora`, and a bounded
 *    wait once the job is actually queued/running.
 *  - It resolved a SINGLE url, while a real job produces many images.
 */
export async function pollJobUntilComplete(
  jobId: string,
  onStatusChange?: (status: JobStatus) => void,
  intervalMs = 8000,
  maxWaitingMinutes = 90, // covers a full training run when parked behind it
  maxRunningMinutes = 30, // generation itself: 172s startup + ~8s/image
): Promise<string[]> {
  const start = Date.now();
  let leftWaiting = false;
  let runningSince = 0;

  for (;;) {
    const { status } = await getJobStatus(jobId);
    onStatusChange?.(status);

    if (status === "completed") return getJobResultUrls(jobId);
    if (status === "failed") throw new Error("Generation failed");

    if (status === "waiting_lora") {
      if (Date.now() - start > maxWaitingMinutes * 60_000) {
        throw new Error("Timed out waiting for your model to finish training");
      }
    } else {
      // First tick after the job actually got picked up — start the shorter clock.
      if (!leftWaiting) {
        leftWaiting = true;
        runningSince = Date.now();
      }
      if (Date.now() - runningSince > maxRunningMinutes * 60_000) {
        throw new Error("Generation timed out");
      }
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}
