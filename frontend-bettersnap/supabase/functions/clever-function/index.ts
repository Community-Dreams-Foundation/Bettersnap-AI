import { createClient } from "npm:@supabase/supabase-js@2";
import { z } from "npm:zod@3.24.2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
};

const jsonHeaders = { ...corsHeaders, "Content-Type": "application/json" };

const BodySchema = z.object({
  image_url: z.string().url(),
  gender: z.string().trim().min(1).max(50).optional(),
  background: z.string().trim().min(1).max(100).optional(),
  category: z.string().trim().min(1).max(100).optional(),
  session_id: z.string().trim().min(1).max(255).optional(),
});

const createJsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: jsonHeaders });

const startReplicatePrediction = async ({
  adminClient,
  jobId,
  imageUrl,
  background,
  category,
  replicateApiKey,
  supabaseUrl,
}: {
  adminClient: ReturnType<typeof createClient>;
  jobId: string;
  imageUrl: string;
  background: string;
  category: string;
  replicateApiKey: string;
  supabaseUrl: string;
}) => {
  try {
    const replicateRes = await fetch("https://api.replicate.com/v1/predictions", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${replicateApiKey}`,
        "Content-Type": "application/json",
        Prefer: "respond-async",
      },
      signal: AbortSignal.timeout(30000),
      body: JSON.stringify({
        version: "a07f252abbbd832009640b27f063ea52d87d7a23a185ca165bec23b5b6571ad9",
        input: {
          image: imageUrl,
          prompt: `Professional ${category} headshot photo, ${background} background, studio lighting, high quality, 8k`,
          negative_prompt: "cartoon, illustration, painting, drawing, anime, blur, noisy",
        },
        webhook: `${supabaseUrl}/functions/v1/headshot-webhook`,
        webhook_events_filter: ["completed"],
      }),
    });

    const prediction = await replicateRes.json().catch(() => null);

    if (!replicateRes.ok || !prediction?.id) {
      console.error("Replicate start error:", prediction);
      await adminClient
        .from("headshot_jobs")
        .update({
          status: "failed",
          error_message: prediction?.detail || prediction?.error || "AI generation failed to start",
          updated_at: new Date().toISOString(),
        })
        .eq("id", jobId);
      return;
    }

    await adminClient
      .from("headshot_jobs")
      .update({
        prediction_id: prediction.id,
        status: "processing",
        updated_at: new Date().toISOString(),
      })
      .eq("id", jobId);
  } catch (error) {
    console.error("Background prediction error:", error);
    await adminClient
      .from("headshot_jobs")
      .update({
        status: "failed",
        error_message: error instanceof Error ? error.message : "Unexpected background error",
        updated_at: new Date().toISOString(),
      })
      .eq("id", jobId);
  }
};

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  try {
    const authHeader = req.headers.get("Authorization");
    if (!authHeader?.startsWith("Bearer ")) {
      return createJsonResponse({ error: "Unauthorized" }, 401);
    }

    const parsedBody = BodySchema.safeParse(await req.json());
    if (!parsedBody.success) {
      return createJsonResponse(
        { error: "Invalid request body", details: parsedBody.error.flatten().fieldErrors },
        400,
      );
    }

    const supabaseUrl = Deno.env.get("SUPABASE_URL");
    const supabaseAnonKey = Deno.env.get("SUPABASE_ANON_KEY");
    const supabaseServiceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
    const replicateApiKey = Deno.env.get("REPLICATE_API_TOKEN");

    if (!supabaseUrl || !supabaseAnonKey || !supabaseServiceRoleKey) {
      return createJsonResponse({ error: "Supabase secrets are not configured" }, 500);
    }

    if (!replicateApiKey) {
      return createJsonResponse({ error: "Replicate API key not configured" }, 500);
    }

    const userClient = createClient(supabaseUrl, supabaseAnonKey, {
      global: { headers: { Authorization: authHeader } },
    });

    const token = authHeader.replace("Bearer ", "");
    const { data: claimsData, error: claimsError } = await userClient.auth.getClaims(token);
    if (claimsError || !claimsData?.claims?.sub) {
      return createJsonResponse({ error: "Invalid token" }, 401);
    }

    const { image_url, gender, background, category, session_id } = parsedBody.data;
    const adminClient = createClient(supabaseUrl, supabaseServiceRoleKey);

    const { data: job, error: jobError } = await adminClient
      .from("headshot_jobs")
      .insert({
        user_id: claimsData.claims.sub,
        input_image_url: image_url,
        gender: gender || "male",
        background: background || "office",
        category: category || "linkedin",
        status: "pending",
        session_id: session_id || crypto.randomUUID(),
      })
      .select("id, session_id")
      .single();

    if (jobError || !job) {
      console.error("Job insert error:", jobError);
      return createJsonResponse({ error: "Failed to create job" }, 500);
    }

    EdgeRuntime.waitUntil(
      startReplicatePrediction({
        adminClient,
        jobId: job.id,
        imageUrl: image_url,
        background: background || "office",
        category: category || "linkedin",
        replicateApiKey,
        supabaseUrl,
      }),
    );

    return createJsonResponse({ success: true, job_id: job.id, session_id: job.session_id }, 202);
  } catch (err) {
    console.error("Unexpected error:", err);
    return createJsonResponse(
      { error: err instanceof Error ? err.message : "Internal server error" },
      500,
    );
  }
});
