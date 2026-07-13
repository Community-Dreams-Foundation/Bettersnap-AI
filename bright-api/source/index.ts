import { serve } from "https://deno.land/std@0.168.0/http/server.ts";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

// ✅ Category → output pixel size
const categorySizeMap: Record<string, { width: number; height: number }> = {
  "linkedin":      { width: 400,  height: 400  },
  "resume":        { width: 400,  height: 400  },
  "university_id": { width: 400,  height: 400  },
  "visa_passport": { width: 600,  height: 600  },
  "opt_cpt":       { width: 600,  height: 600  },
};

const DEFAULT_SIZE = { width: 400, height: 400 };

serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  try {
    const supabase = createClient(
      Deno.env.get("SUPABASE_URL") ?? "",
      Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? ""
    );

    const body = await req.json();
    console.log("Webhook received:", JSON.stringify(body));

    const prediction_id = body.id;
    const status = body.status;
    const output = body.output;
    const error = body.error;

    if (!prediction_id) {
      return new Response("Missing prediction_id", { status: 400 });
    }

    if (status === "succeeded" && output) {
      const rawOutputUrl = Array.isArray(output) ? output[0] : output;

      // ✅ Step 1 - Get the job from DB to find category
      const { data: job, error: jobFetchError } = await supabase
        .from("headshot_jobs")
        .select("id, category")
        .eq("prediction_id", prediction_id)
        .single();

      if (jobFetchError || !job) {
        console.error("Failed to fetch job:", jobFetchError);
        return new Response("Job not found", { status: 404 });
      }

      const category = job.category ?? "linkedin";
      const size = categorySizeMap[category] ?? DEFAULT_SIZE;
      console.log(`Category: ${category}, resizing to ${size.width}x${size.height}`);

      // ✅ Step 2 - Fetch the raw image from Replicate
      const imageRes = await fetch(rawOutputUrl);
      if (!imageRes.ok) {
        console.error("Failed to fetch image from Replicate");
        return new Response("Image fetch failed", { status: 500 });
      }
      const imageBuffer = await imageRes.arrayBuffer();

      // ✅ Step 3 - Resize using Deno's ImageMagick (via magick WASM)
      const resizedBuffer = await resizeImage(imageBuffer, size.width, size.height);

      // ✅ Step 4 - Upload resized image to Supabase Storage
      const fileName = `output/${job.id}_${category}_${size.width}x${size.height}.jpg`;
      const { error: uploadError } = await supabase.storage
        .from("Headshotsd")
        .upload(fileName, resizedBuffer, {
          contentType: "image/jpeg",
          upsert: true,
        });

      if (uploadError) {
        console.error("Upload error:", uploadError);
        // Fallback — save original URL if upload fails
        await supabase
          .from("headshot_jobs")
          .update({
            status: "succeeded",
            output_image_url: rawOutputUrl,
            updated_at: new Date().toISOString(),
          })
          .eq("prediction_id", prediction_id);
        return new Response("ok", { status: 200 });
      }

      // ✅ Step 5 - Get public URL of resized image
      const { data: publicUrlData } = supabase.storage
  .from("Headshotsd")
  .getPublicUrl(fileName);

      const finalUrl = publicUrlData.publicUrl;
      console.log("Resized image uploaded:", finalUrl);

      // ✅ Step 6 - Save final URL to DB
      const { error: dbError } = await supabase
        .from("headshot_jobs")
        .update({
          status: "succeeded",
          output_image_url: finalUrl,
          updated_at: new Date().toISOString(),
        })
        .eq("prediction_id", prediction_id);

      if (dbError) {
        console.error("DB update error:", dbError);
        return new Response("DB error", { status: 500 });
      }

      console.log("Job succeeded, resized output saved:", finalUrl);
    }

    if (status === "failed" || status === "canceled") {
      const { error: dbError } = await supabase
        .from("headshot_jobs")
        .update({
          status: "failed",
          error_message: error || "Prediction failed",
          updated_at: new Date().toISOString(),
        })
        .eq("prediction_id", prediction_id);

      if (dbError) {
        console.error("DB update error:", dbError);
        return new Response("DB error", { status: 500 });
      }

      console.log("Job failed:", error);
    }

    return new Response("ok", { status: 200 });

  } catch (err) {
    console.error("Webhook error:", err);
    return new Response("Internal error", { status: 500 });
  }
});

// ✅ Resize image using Canvas API (available in Deno)
async function resizeImage(
  buffer: ArrayBuffer,
  width: number,
  height: number
): Promise<Uint8Array> {
  try {
    // Use Deno's built-in createImageBitmap + OffscreenCanvas
    const blob = new Blob([buffer], { type: "image/jpeg" });
    const imageBitmap = await createImageBitmap(blob);

    const canvas = new OffscreenCanvas(width, height);
    const ctx = canvas.getContext("2d");

    ctx!.drawImage(imageBitmap, 0, 0, width, height);

    const resizedBlob = await canvas.convertToBlob({ type: "image/jpeg", quality: 0.95 });
    const resizedBuffer = await resizedBlob.arrayBuffer();
    return new Uint8Array(resizedBuffer);
  } catch (err) {
    console.error("Resize failed, returning original:", err);
    return new Uint8Array(buffer);
  }
}