import { createClient } from "npm:@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
};

const MODAL_API_URL = "https://sivmm29--better-snap-ai-headshotgenerator-generate.modal.run";

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  try {
    const authHeader = req.headers.get("Authorization");
    if (!authHeader?.startsWith("Bearer ")) {
      return new Response(
        JSON.stringify({ error: "Unauthorized" }),
        { status: 401, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
    const supabaseAnonKey = Deno.env.get("SUPABASE_ANON_KEY")!;
    const supabaseServiceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

    const userClient = createClient(supabaseUrl, supabaseAnonKey, {
      global: { headers: { Authorization: authHeader } },
    });

    const token = authHeader.replace("Bearer ", "");
    const { data: { user }, error: userError } = await userClient.auth.getUser(token);
    if (userError || !user) {
      return new Response(
        JSON.stringify({ error: "Invalid token" }),
        { status: 401, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    const userId = user.id;
    const { image_url, gender, background, category } = await req.json();

    if (!image_url) {
      return new Response(
        JSON.stringify({ error: "Missing image_url" }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    const adminClient = createClient(supabaseUrl, supabaseServiceRoleKey);
    const sessionId = crypto.randomUUID();

    // Call Modal endpoint
    const modalRes = await fetch(MODAL_API_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        image_url,
        gender: gender || "male",
        background: background || "light_grey",
        category: category || "linkedin",
        num_images: 4,
      }),
    });

    if (!modalRes.ok) {
      const errText = await modalRes.text();
      return new Response(
        JSON.stringify({ error: "Modal failed", status: modalRes.status, details: errText.slice(0, 200) }),
        { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    const modalData = await modalRes.json();
    const images = modalData.images || [];

    if (images.length === 0) {
      return new Response(
        JSON.stringify({ error: "No images returned from Modal" }),
        { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    const signedUrls: string[] = [];

    for (let i = 0; i < images.length; i++) {
      const base64 = images[i];
      const buffer = Uint8Array.from(atob(base64), c => c.charCodeAt(0));
      const filePath = `${userId}/${sessionId}/variation_${i + 1}.png`;

      // Upload to private bucket
      const { error: uploadError } = await adminClient.storage
        .from("Headshotsd")
        .upload(filePath, buffer, { contentType: "image/png", upsert: true });

      if (uploadError) {
        return new Response(
          JSON.stringify({ error: "Storage upload failed", details: uploadError.message }),
          { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
        );
      }

      // Create signed URL valid for 24 hours
      const { data: signedData, error: signedError } = await adminClient.storage
        .from("Headshotsd")
        .createSignedUrl(filePath, 60 * 60 * 24);

      if (signedError || !signedData?.signedUrl) {
        return new Response(
          JSON.stringify({ error: "Failed to create signed URL", details: signedError?.message }),
          { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
        );
      }

      signedUrls.push(signedData.signedUrl);

      // Store file path in DB (not the signed URL — paths don't expire)
      await adminClient
        .from("headshot_jobs")
        .insert({
          user_id: userId,
          session_id: sessionId,
          input_image_url: image_url,
          output_image_url: signedData.signedUrl,
          gender: gender || "male",
          background: background || "light_grey",
          category: category || "linkedin",
          status: "succeeded",
        });
    }

    return new Response(
      JSON.stringify({ success: true, session_id: sessionId, images: signedUrls }),
      { status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );

  } catch (err) {
    console.error("Unexpected error:", err);
    return new Response(
      JSON.stringify({ error: err.message || "Internal server error" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  }
});