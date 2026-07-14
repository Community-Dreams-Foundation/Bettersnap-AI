import { toast } from "sonner";
import { createCheckout, type PaymentType } from "@/lib/azureApi";

export type PlanId = "trial" | "basic" | "pro" | "expert";
export type PlanType = PaymentType; // "monthly" | "one_time"

// TODO: Connect pricing buttons to Stripe Checkout once the approved checkout endpoint is available.
// If the backend checkout endpoint is live, `createCheckout` returns a URL and we redirect.
// Otherwise we show a friendly "coming soon" toast so the UI never breaks.
export async function handlePlanSelect(
  planType: PlanType,
  planId: PlanId,
): Promise<void> {
  // Free trial doesn't go through checkout.
  if (planId === "trial") {
    toast.info("Free trial is coming soon. Please sign up to be notified.");
    return;
  }

  if (typeof createCheckout !== "function") {
    toast.info("Checkout is coming soon. Stripe payment integration is being finalized.");
    return;
  }

  try {
    const res = await createCheckout(planId, planType);
    const url = res?.checkout_url;
    if (url && typeof url === "string") {
      window.location.href = url;
      return;
    }
    toast.info("Checkout is coming soon. Stripe payment integration is being finalized.");
  } catch {
    toast.info("Checkout is coming soon. Stripe payment integration is being finalized.");
  }
}
