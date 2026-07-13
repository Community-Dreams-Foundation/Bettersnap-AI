import { createClient } from "npm:@supabase/supabase-js@2";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  try {
    const supabaseUrl = Deno.env.get("SUPABASE_URL")!;
    const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
    const adminClient = createClient(supabaseUrl, serviceRoleKey);

    // Get all jobs older than 24 hours that still have images
    const { data: jobs, error } = await adminClient
      .from("headshot_jobs")
      .select("id, input_image_url, output_image_url")
      .lt("created_at", new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString())
      .or("input_image_url.not.is.null,output_image_url.not.is.null");

    if (error) {
      console.error("Query error:", error);
      return new Response(
        JSON.stringify({ error: error.message }),
        { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    if (!jobs || jobs.length === 0) {
      return new Response(
        JSON.stringify({ message: "No jobs to clean up", deleted: 0 }),
        { status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    // Extract storage paths from URLs
    const STORAGE_PREFIX = `${supabaseUrl}/storage/v1/object/public/Headshotsd/`;
    const paths: string[] = [];

    for (const job of jobs) {
      if (job.input_image_url) {
        const path = job.input_image_url
          .replace(STORAGE_PREFIX, "")
          .split("?")[0]; // strip signed URL query params if any
        if (path) paths.push(path);
      }
      if (job.output_image_url) {
        const path = job.output_image_url
          .replace(STORAGE_PREFIX, "")
          .split("?")[0];
        if (path) paths.push(path);
      }
    }

    // Delete files from storage
    if (paths.length > 0) {
      const { error: storageError } = await adminClient.storage
        .from("Headshotsd")
        .remove(paths);

      if (storageError) {
        console.error("Storage deletion error:", storageError);
      }
    }

    // Null out the URLs in the table
    const ids = jobs.map((j) => j.id);
    const { error: updateError } = await adminClient
      .from("headshot_jobs")
      .update({ input_image_url: null, output_image_url: null })
      .in("id", ids);

    if (updateError) {
      console.error("Table update error:", updateError);
    }

    console.log(`Cleaned up ${paths.length} files from ${jobs.length} jobs`);

    return new Response(
      JSON.stringify({
        success: true,
        jobs_processed: jobs.length,
        files_deleted: paths.length,
      }),
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