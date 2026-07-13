/**
 * Image preprocessing pipeline: adaptive enhancement based on blur severity.
 * Sharpen, normalize brightness/contrast, deblur, and face-visibility check.
 */

import { type BlurSeverity, detectBlur } from "./blur-detection";

interface PreprocessResult {
  file: File;
  hasFace: boolean;
  enhanced: boolean;       // true if enhancement was applied
  blurSeverity: BlurSeverity;
}

/**
 * Preprocess an image file with adaptive enhancement based on detected blur level.
 * - Sharp images: light normalization only
 * - Slight blur: moderate sharpening + normalization
 * - Moderate blur: aggressive sharpening + deblur pass + normalization
 * - Severe blur: rejected (hasFace = false, shouldReject flag)
 */
export async function preprocessImage(file: File): Promise<PreprocessResult> {
  const img = await loadImage(file);
  const canvas = document.createElement("canvas");
  canvas.width = img.width;
  canvas.height = img.height;
  const ctx = canvas.getContext("2d")!;
  ctx.drawImage(img, 0, 0);

  // Detect blur severity on the raw image
  const blurResult = detectBlur(canvas);

  // Severe blur → reject immediately
  if (blurResult.shouldReject) {
    return {
      file,
      hasFace: false,
      enhanced: false,
      blurSeverity: "severe",
    };
  }

  // Determine enhancement intensity based on blur severity
  const intensity = getEnhancementIntensity(blurResult.severity);

  // 1. Normalize brightness & contrast
  normalizeBrightnessContrast(ctx, canvas.width, canvas.height);

  // 2. Apply deblur pass for moderate blur (iterative sharpening)
  if (intensity.deblurPasses > 0) {
    for (let i = 0; i < intensity.deblurPasses; i++) {
      deblurPass(ctx, canvas.width, canvas.height);
    }
  }

  // 3. Apply sharpening filter with adaptive strength
  sharpen(ctx, canvas.width, canvas.height, intensity.sharpenAmount);

  // 4. Apply edge-aware contrast enhancement for blurry inputs
  if (blurResult.severity !== "sharp") {
    localContrastEnhance(ctx, canvas.width, canvas.height, intensity.contrastBoost);
  }

  // 5. Check face visibility
  const hasFace = detectFaceRegion(ctx, canvas.width, canvas.height);

  // Convert back to file
  const blob = await canvasToBlob(canvas, file.type || "image/jpeg");
  const enhanced = new File([blob], file.name, { type: file.type || "image/jpeg" });

  return {
    file: enhanced,
    hasFace,
    enhanced: blurResult.severity !== "sharp",
    blurSeverity: blurResult.severity,
  };
}

interface EnhancementIntensity {
  sharpenAmount: number;
  deblurPasses: number;
  contrastBoost: number;
}

function getEnhancementIntensity(severity: BlurSeverity): EnhancementIntensity {
  switch (severity) {
    case "sharp":
      return { sharpenAmount: 0.3, deblurPasses: 0, contrastBoost: 0 };
    case "slight":
      return { sharpenAmount: 0.6, deblurPasses: 0, contrastBoost: 0.15 };
    case "moderate":
      return { sharpenAmount: 0.9, deblurPasses: 2, contrastBoost: 0.25 };
    case "severe":
      return { sharpenAmount: 0, deblurPasses: 0, contrastBoost: 0 };
  }
}

function loadImage(file: File): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(img.src);
      resolve(img);
    };
    img.onerror = reject;
    img.src = URL.createObjectURL(file);
  });
}

function canvasToBlob(canvas: HTMLCanvasElement, type: string): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) => (blob ? resolve(blob) : reject(new Error("Canvas to blob failed"))),
      type === "image/png" ? "image/png" : "image/jpeg",
      0.92
    );
  });
}

/**
 * Auto-level brightness & contrast using histogram stretching with percentile clipping.
 */
function normalizeBrightnessContrast(ctx: CanvasRenderingContext2D, w: number, h: number) {
  const imageData = ctx.getImageData(0, 0, w, h);
  const data = imageData.data;

  const histogram = new Uint32Array(256);
  for (let i = 0; i < data.length; i += 4) {
    const l = Math.round(0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2]);
    histogram[l]++;
  }

  const totalPixels = w * h;
  const clipLow = totalPixels * 0.01;
  const clipHigh = totalPixels * 0.99;
  let cumulative = 0;
  let lowBound = 0, highBound = 255;

  for (let i = 0; i < 256; i++) {
    cumulative += histogram[i];
    if (cumulative >= clipLow) { lowBound = i; break; }
  }
  cumulative = 0;
  for (let i = 255; i >= 0; i--) {
    cumulative += histogram[i];
    if (cumulative >= totalPixels - clipHigh) { highBound = i; break; }
  }

  const range = highBound - lowBound || 1;

  if (range < 200) {
    for (let i = 0; i < data.length; i += 4) {
      data[i] = Math.min(255, Math.max(0, ((data[i] - lowBound) / range) * 255));
      data[i + 1] = Math.min(255, Math.max(0, ((data[i + 1] - lowBound) / range) * 255));
      data[i + 2] = Math.min(255, Math.max(0, ((data[i + 2] - lowBound) / range) * 255));
    }
    ctx.putImageData(imageData, 0, 0);
  }
}

/**
 * Adaptive unsharp mask sharpening with configurable strength.
 */
function sharpen(ctx: CanvasRenderingContext2D, w: number, h: number, amount: number) {
  const imageData = ctx.getImageData(0, 0, w, h);
  const src = new Uint8ClampedArray(imageData.data);
  const dst = imageData.data;

  for (let y = 1; y < h - 1; y++) {
    for (let x = 1; x < w - 1; x++) {
      const idx = (y * w + x) * 4;
      for (let c = 0; c < 3; c++) {
        const center = src[idx + c];
        const neighbors =
          src[((y - 1) * w + x) * 4 + c] +
          src[((y + 1) * w + x) * 4 + c] +
          src[(y * w + (x - 1)) * 4 + c] +
          src[(y * w + (x + 1)) * 4 + c];
        const blur = neighbors / 4;
        const sharpened = center + (center - blur) * amount;
        dst[idx + c] = Math.min(255, Math.max(0, sharpened));
      }
    }
  }

  ctx.putImageData(imageData, 0, 0);
}

/**
 * Deblur pass: iterative Richardson-Lucy-style deconvolution approximation.
 * Uses a single-pass Wiener-like approach for speed.
 */
function deblurPass(ctx: CanvasRenderingContext2D, w: number, h: number) {
  const imageData = ctx.getImageData(0, 0, w, h);
  const src = new Uint8ClampedArray(imageData.data);
  const dst = imageData.data;

  // 5x5 weighted average for blur estimation, then boost difference
  for (let y = 2; y < h - 2; y++) {
    for (let x = 2; x < w - 2; x++) {
      const idx = (y * w + x) * 4;
      for (let c = 0; c < 3; c++) {
        const center = src[idx + c];
        // 5x5 cross average
        let blurEstimate = 0;
        let count = 0;
        for (let dy = -2; dy <= 2; dy++) {
          for (let dx = -2; dx <= 2; dx++) {
            blurEstimate += src[((y + dy) * w + (x + dx)) * 4 + c];
            count++;
          }
        }
        blurEstimate /= count;

        // Boost the difference between original and blur estimate
        const detail = center - blurEstimate;
        const restored = center + detail * 0.7;
        dst[idx + c] = Math.min(255, Math.max(0, restored));
      }
    }
  }

  ctx.putImageData(imageData, 0, 0);
}

/**
 * Local contrast enhancement: boosts micro-contrast to recover detail in soft images.
 */
function localContrastEnhance(ctx: CanvasRenderingContext2D, w: number, h: number, boost: number) {
  if (boost <= 0) return;

  const imageData = ctx.getImageData(0, 0, w, h);
  const src = new Uint8ClampedArray(imageData.data);
  const dst = imageData.data;

  // Use a larger kernel (7x7 approximation via 2-pass) for local mean
  const kernelSize = 3;

  for (let y = kernelSize; y < h - kernelSize; y++) {
    for (let x = kernelSize; x < w - kernelSize; x++) {
      const idx = (y * w + x) * 4;
      for (let c = 0; c < 3; c++) {
        const center = src[idx + c];
        // Local mean
        let localSum = 0;
        let count = 0;
        for (let dy = -kernelSize; dy <= kernelSize; dy++) {
          for (let dx = -kernelSize; dx <= kernelSize; dx++) {
            localSum += src[((y + dy) * w + (x + dx)) * 4 + c];
            count++;
          }
        }
        const localMean = localSum / count;
        const enhanced = center + (center - localMean) * boost;
        dst[idx + c] = Math.min(255, Math.max(0, enhanced));
      }
    }
  }

  ctx.putImageData(imageData, 0, 0);
}

/**
 * Face-region detection using skin-tone pixel density heuristic.
 */
function detectFaceRegion(ctx: CanvasRenderingContext2D, w: number, h: number): boolean {
  const cx = Math.round(w * 0.2);
  const cy = Math.round(h * 0.1);
  const cw = Math.round(w * 0.6);
  const ch = Math.round(h * 0.7);

  const imageData = ctx.getImageData(cx, cy, cw, ch);
  const data = imageData.data;
  const totalPixels = cw * ch;

  let skinPixels = 0;
  for (let i = 0; i < data.length; i += 4) {
    const r = data[i], g = data[i + 1], b = data[i + 2];
    if (isSkinTone(r, g, b)) skinPixels++;
  }

  const skinRatio = skinPixels / totalPixels;
  return skinRatio >= 0.05;
}

function isSkinTone(r: number, g: number, b: number): boolean {
  return (
    r > 60 && g > 40 && b > 20 &&
    r > g && r > b &&
    Math.abs(r - g) > 10 &&
    r - b > 15 &&
    r < 250 && g < 230
  );
}
