import { createClient } from "npm:@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers":
    "authorization, x-client-info, apikey, content-type",
};

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  try {
    const body = await req.json();
    console.log("Webhook received:", JSON.stringify(body, null, 2));

    const predictionId = body.id;
    const status = body.status;
    const output = body.output;
    const error = body.error;

    if (!predictionId) {
      return new Response(
        JSON.stringify({ error: "Missing prediction ID" }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    const adminClient = createClient(
      Deno.env.get("SUPABASE_URL")!,
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!
    );

    if (status === "failed" || error) {
      await adminClient
        .from("headshot_jobs")
        .update({
          status: "failed",
          error_message: error || "Generation failed",
          updated_at: new Date().toISOString(),
        })
        .eq("prediction_id", predictionId);

      return new Response(
        JSON.stringify({ success: true, status: "failed" }),
        { status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    if (status === "succeeded" && output) {
      // Replicate output can be a string or array
      const imageUrl = Array.isArray(output) ? output[0] : output;

      if (!imageUrl) {
        await adminClient
          .from("headshot_jobs")
          .update({
            status: "failed",
            error_message: "No output image from AI",
            updated_at: new Date().toISOString(),
          })
          .eq("prediction_id", predictionId);

        return new Response(
          JSON.stringify({ success: true, status: "no_output" }),
          { status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" } }
        );
      }

      // Download the image from Replicate
      const imageRes = await fetch(imageUrl);
      if (!imageRes.ok) {
        throw new Error(`Failed to download image: ${imageRes.status}`);
      }
      const imageBlob = await imageRes.blob();
      const imageBuffer = new Uint8Array(await imageBlob.arrayBuffer());

      // Determine file extension
      const contentType = imageRes.headers.get("content-type") || "image/png";
      const ext = contentType.includes("jpeg") || contentType.includes("jpg") ? "jpg" : "png";
      const fileName = `output_${predictionId}_${Date.now()}.${ext}`;
      const storagePath = `Headshots/${fileName}`;

      // Upload to Supabase Storage
      const { error: uploadError } = await adminClient.storage
        .from("Headshotsd")
        .upload(storagePath, imageBuffer, {
          contentType,
          upsert: true,
        });

      if (uploadError) {
        console.error("Storage upload error:", uploadError);
        // Fall back to the temporary Replicate URL
        await adminClient
          .from("headshot_jobs")
          .update({
            status: "succeeded",
            output_image_url: imageUrl,
            updated_at: new Date().toISOString(),
          })
          .eq("prediction_id", predictionId);

        return new Response(
          JSON.stringify({ success: true, status: "succeeded", storage: "fallback" }),
          { status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" } }
        );
      }

      // Get permanent public URL
      const { data: urlData } = adminClient.storage
        .from("Headshotsd")
        .getPublicUrl(storagePath);

      const permanentUrl = urlData.publicUrl;

      // Update the job with the permanent URL
      await adminClient
        .from("headshot_jobs")
        .update({
          status: "succeeded",
          output_image_url: permanentUrl,
          updated_at: new Date().toISOString(),
        })
        .eq("prediction_id", predictionId);

      console.log(`Image saved permanently: ${permanentUrl}`);

      return new Response(
        JSON.stringify({ success: true, status: "succeeded", url: permanentUrl }),
        { status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    // Other statuses — just acknowledge
    return new Response(
      JSON.stringify({ success: true, status }),
      { status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  } catch (err) {
    console.error("Webhook error:", err);
    return new Response(
      JSON.stringify({ error: err.message || "Internal server error" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  }
});
