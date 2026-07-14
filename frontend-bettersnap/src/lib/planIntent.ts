// Small helper for remembering which pricing plan a signed-out user clicked on
// the landing page, so we can restore that intent after they finish
// login/signup and land on the Billing page.
//
// This is UI-only state. It never touches auth, API payloads, or backend logic.

export type PlanIntentType = "monthly" | "one_time";
export type PlanIntentId = "trial" | "basic" | "pro" | "expert";

export interface PlanIntent {
  planType: PlanIntentType;
  planId: PlanIntentId;
  source: string;
  savedAt: number;
}

const KEY = "bettersnap:selected_plan_intent";

export function setPlanIntent(intent: Omit<PlanIntent, "savedAt">): void {
  try {
    const payload: PlanIntent = { ...intent, savedAt: Date.now() };
    sessionStorage.setItem(KEY, JSON.stringify(payload));
  } catch {
    // storage unavailable — safe no-op
  }
}

export function getPlanIntent(): PlanIntent | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as PlanIntent;
    if (!parsed?.planType || !parsed?.planId) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function clearPlanIntent(): void {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    // ignore
  }
}

export function hasPlanIntent(): boolean {
  return getPlanIntent() !== null;
}
