/**
 * Low-light detection via average perceived brightness on canvas pixel data.
 * Returns 0-255 average luminance and a `isLowLight` flag.
 */

// Average luminance below this is considered too dark for AI headshot generation.
export const LOW_LIGHT_THRESHOLD = 60;

export interface BrightnessResult {
  averageBrightness: number; // 0-255
  isLowLight: boolean;
}

export function detectBrightness(canvas: HTMLCanvasElement): BrightnessResult {
  const ctx = canvas.getContext("2d");
  if (!ctx) return { averageBrightness: 255, isLowLight: false };

  // Downsample for speed
  const maxDim = 200;
  const scale = Math.min(maxDim / canvas.width, maxDim / canvas.height, 1);
  const w = Math.max(1, Math.round(canvas.width * scale));
  const h = Math.max(1, Math.round(canvas.height * scale));

  const tmp = document.createElement("canvas");
  tmp.width = w;
  tmp.height = h;
  const tmpCtx = tmp.getContext("2d")!;
  tmpCtx.drawImage(canvas, 0, 0, w, h);

  const { data } = tmpCtx.getImageData(0, 0, w, h);
  let sum = 0;
  const px = w * h;
  for (let i = 0; i < px; i++) {
    const idx = i * 4;
    // Perceived luminance (Rec. 601)
    sum += 0.299 * data[idx] + 0.587 * data[idx + 1] + 0.114 * data[idx + 2];
  }
  const avg = sum / px;
  return {
    averageBrightness: Math.round(avg * 100) / 100,
    isLowLight: avg < LOW_LIGHT_THRESHOLD,
  };
}

export function detectBrightnessFromFile(file: File): Promise<BrightnessResult> {
  return new Promise((resolve) => {
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => {
      const canvas = document.createElement("canvas");
      canvas.width = img.width;
      canvas.height = img.height;
      const ctx = canvas.getContext("2d")!;
      ctx.drawImage(img, 0, 0);
      URL.revokeObjectURL(url);
      resolve(detectBrightness(canvas));
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      resolve({ averageBrightness: 255, isLowLight: false });
    };
    img.src = url;
  });
}
