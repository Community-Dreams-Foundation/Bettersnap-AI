/**
 * Blur detection using Laplacian variance on canvas image data.
 * Supports tiered severity (sharp, slight, moderate, severe) and face clarity scoring.
 */

export type BlurSeverity = "sharp" | "slight" | "moderate" | "severe";

// Thresholds tuned for headshot photos (256px downsampled)
const SEVERE_THRESHOLD = 8;    // Below = unusable
const MODERATE_THRESHOLD = 15; // Below = needs significant enhancement
const SLIGHT_THRESHOLD = 30;   // Below = light enhancement helpful
// Above SLIGHT_THRESHOLD = sharp

export interface BlurResult {
  isBlurry: boolean;
  severity: BlurSeverity;
  variance: number;
  faceClarity: number;       // 0-100 score for face region sharpness
  canEnhance: boolean;       // true if slight/moderate (auto-enhanceable)
  shouldReject: boolean;     // true if severe (unusable)
}

export function classifyBlur(variance: number): BlurSeverity {
  if (variance < SEVERE_THRESHOLD) return "severe";
  if (variance < MODERATE_THRESHOLD) return "moderate";
  if (variance < SLIGHT_THRESHOLD) return "slight";
  return "sharp";
}

export function detectBlur(
  canvas: HTMLCanvasElement
): BlurResult {
  const ctx = canvas.getContext("2d");
  if (!ctx) return defaultResult();

  // Downsample for performance
  const maxDim = 256;
  const scale = Math.min(maxDim / canvas.width, maxDim / canvas.height, 1);
  const w = Math.round(canvas.width * scale);
  const h = Math.round(canvas.height * scale);

  const tmp = document.createElement("canvas");
  tmp.width = w;
  tmp.height = h;
  const tmpCtx = tmp.getContext("2d")!;
  tmpCtx.drawImage(canvas, 0, 0, w, h);

  const imageData = tmpCtx.getImageData(0, 0, w, h);
  const gray = toGrayscale(imageData.data, w, h);
  const variance = laplacianVariance(gray, w, h);

  // Face region clarity — check center 60% of image (where face typically is)
  const faceClarity = computeFaceClarity(gray, w, h);

  const severity = classifyBlur(variance);
  const isBlurry = severity !== "sharp";
  const canEnhance = severity === "slight" || severity === "moderate";
  const shouldReject = severity === "severe";

  return {
    isBlurry,
    severity,
    variance: Math.round(variance * 100) / 100,
    faceClarity,
    canEnhance,
    shouldReject,
  };
}

export function detectBlurFromFile(
  file: File
): Promise<BlurResult> {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => {
      const canvas = document.createElement("canvas");
      canvas.width = img.width;
      canvas.height = img.height;
      const ctx = canvas.getContext("2d")!;
      ctx.drawImage(img, 0, 0);
      resolve(detectBlur(canvas));
      URL.revokeObjectURL(img.src);
    };
    img.onerror = () => {
      URL.revokeObjectURL(img.src);
      resolve(defaultResult());
    };
    img.src = URL.createObjectURL(file);
  });
}

function defaultResult(): BlurResult {
  return {
    isBlurry: false,
    severity: "sharp",
    variance: 0,
    faceClarity: 100,
    canEnhance: false,
    shouldReject: false,
  };
}

/**
 * Compute face-region sharpness as a 0-100 score.
 * Uses Laplacian variance on the center crop where the face is expected.
 */
function computeFaceClarity(gray: Float32Array, w: number, h: number): number {
  const cx = Math.round(w * 0.2);
  const cy = Math.round(h * 0.1);
  const cw = Math.round(w * 0.6);
  const ch = Math.round(h * 0.6);

  // Extract center region
  const faceGray = new Float32Array(cw * ch);
  for (let y = 0; y < ch; y++) {
    for (let x = 0; x < cw; x++) {
      faceGray[y * cw + x] = gray[(cy + y) * w + (cx + x)];
    }
  }

  const faceVariance = laplacianVariance(faceGray, cw, ch);
  // Map variance to 0-100 score (50+ is sharp, 80+ is excellent)
  const score = Math.min(100, Math.round((faceVariance / 50) * 100));
  return Math.max(0, score);
}

function toGrayscale(data: Uint8ClampedArray, w: number, h: number): Float32Array {
  const gray = new Float32Array(w * h);
  for (let i = 0; i < w * h; i++) {
    const idx = i * 4;
    gray[i] = 0.299 * data[idx] + 0.587 * data[idx + 1] + 0.114 * data[idx + 2];
  }
  return gray;
}

function laplacianVariance(gray: Float32Array, w: number, h: number): number {
  let sum = 0;
  let sumSq = 0;
  let count = 0;

  for (let y = 1; y < h - 1; y++) {
    for (let x = 1; x < w - 1; x++) {
      const lap =
        gray[(y - 1) * w + x] +
        gray[(y + 1) * w + x] +
        gray[y * w + (x - 1)] +
        gray[y * w + (x + 1)] -
        4 * gray[y * w + x];
      sum += lap;
      sumSq += lap * lap;
      count++;
    }
  }

  const mean = sum / count;
  return sumSq / count - mean * mean;
}
