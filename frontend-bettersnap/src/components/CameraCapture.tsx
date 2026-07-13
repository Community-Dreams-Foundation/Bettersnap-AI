import { useState, useRef, useCallback, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Camera, RotateCcw, Check, X, AlertTriangle } from "lucide-react";
import { motion } from "framer-motion";
import { detectBlur } from "@/lib/blur-detection";
import { detectBrightness } from "@/lib/brightness-detection";
import ImageCropper from "@/components/ImageCropper";

interface CameraCaptureProps {
  onCapture: (file: File) => void;
  onClose: () => void;
}

const CameraCapture = ({ onCapture, onClose }: CameraCaptureProps) => {
  const videoRef = useRef<HTMLVideoElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const [captured, setCaptured] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [blurWarning, setBlurWarning] = useState(false);
  const [lowLightWarning, setLowLightWarning] = useState(false);
  const [showCropper, setShowCropper] = useState(false);
  const [capturedDataUrl, setCapturedDataUrl] = useState<string | null>(null);

  const stopCamera = useCallback(() => {
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
    }
    if (videoRef.current) {
      videoRef.current.srcObject = null;
    }
  }, []);

  const startCamera = useCallback(async () => {
    stopCamera();
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "user", width: { ideal: 1280 }, height: { ideal: 1280 } },
        audio: false,
      });
      streamRef.current = stream;
      if (videoRef.current) {
        videoRef.current.srcObject = stream;
      }
    } catch {
      setError("Camera access denied. Please use file upload instead.");
    }
  }, [stopCamera]);

  useEffect(() => {
    if (!captured && videoRef.current && streamRef.current) {
      videoRef.current.srcObject = streamRef.current;
    }
  }, [captured]);

  useEffect(() => {
    startCamera();
    return () => stopCamera();
  }, [startCamera, stopCamera]);

  const takePhoto = () => {
    const video = videoRef.current;
    const canvas = canvasRef.current;
    if (!video || !canvas) return;

    const size = Math.min(video.videoWidth, video.videoHeight);
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext("2d")!;
    const ox = (video.videoWidth - size) / 2;
    const oy = (video.videoHeight - size) / 2;
    ctx.drawImage(video, ox, oy, size, size, 0, 0, size, size);

    const result = detectBlur(canvas);
    setBlurWarning(result.shouldReject);

    const brightness = detectBrightness(canvas);
    setLowLightWarning(brightness.isLowLight);

    const dataUrl = canvas.toDataURL("image/jpeg", 0.92);
    setCaptured(dataUrl);
    setCapturedDataUrl(dataUrl);
    stopCamera();
  };

  const retake = () => {
    setCaptured(null);
    setCapturedDataUrl(null);
    setBlurWarning(false);
    setLowLightWarning(false);
    setShowCropper(false);
    startCamera();
  };

  const handleCropComplete = (croppedFile: File) => {
    setShowCropper(false);
    stopCamera();
    onCapture(croppedFile);
  };

  const confirm = () => {
    if (!canvasRef.current) return;
    canvasRef.current.toBlob(
      (blob) => {
        if (blob) {
          const file = new File([blob], `camera-${Date.now()}.jpg`, { type: "image/jpeg" });
          stopCamera();
          onCapture(file);
        }
      },
      "image/jpeg",
      0.92,
    );
  };

  const handleClose = () => {
    stopCamera();
    onClose();
  };

  if (error) {
    return (
      <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="glass rounded-2xl p-8 text-center space-y-4">
        <Camera className="w-10 h-10 text-muted-foreground mx-auto" />
        <p className="text-foreground font-medium">{error}</p>
        <Button variant="outline" onClick={handleClose} className="glass border-border text-foreground">
          Use File Upload
        </Button>
      </motion.div>
    );
  }

  // Show cropper when user wants to crop the captured photo
  if (showCropper && capturedDataUrl) {
    return (
      <ImageCropper
        imageSrc={capturedDataUrl}
        onCropComplete={handleCropComplete}
        onCancel={() => setShowCropper(false)}
      />
    );
  }

  return (
    <motion.div initial={{ opacity: 0, scale: 0.95 }} animate={{ opacity: 1, scale: 1 }} className="glass rounded-2xl overflow-hidden shadow-glass">
      <div className="relative">
        <canvas ref={canvasRef} className="hidden" />

        {!captured ? (
          <video
            ref={videoRef}
            autoPlay
            playsInline
            muted
            className="w-full aspect-square object-cover rounded-t-2xl"
            style={{ transform: "scaleX(-1)" }}
          />
        ) : (
          <img src={captured} alt="Captured photo" className="w-full aspect-square object-cover rounded-t-2xl" style={{ transform: "scaleX(-1)" }} />
        )}

        <button
          onClick={handleClose}
          className="absolute top-3 right-3 w-8 h-8 rounded-full bg-black/50 flex items-center justify-center text-white hover:bg-black/70 transition-colors"
          aria-label="Close camera"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* Blur warning */}
      {blurWarning && captured && (
        <div className="flex items-center gap-2 px-4 pt-3 text-amber-400">
          <AlertTriangle className="w-4 h-4 shrink-0" />
          <p className="text-sm font-medium">Image looks blurry. Please retake for best results.</p>
        </div>
      )}

      {/* Low-light warning */}
      {lowLightWarning && captured && (
        <div className="flex items-center gap-2 px-4 pt-3 text-amber-400">
          <AlertTriangle className="w-4 h-4 shrink-0" />
          <p className="text-sm font-medium">Lighting is too low. Please take the photo in better lighting.</p>
        </div>
      )}

      <div className="flex justify-center gap-3 p-4">
        {!captured ? (
          <Button onClick={takePhoto} size="lg" className="gradient-cta text-primary-foreground font-semibold hover-scale btn-glow rounded-full w-16 h-16 p-0">
            <Camera className="w-6 h-6" />
          </Button>
        ) : (
          <>
            <Button onClick={retake} variant="outline" size="lg" className="glass border-border text-foreground">
              <RotateCcw className="w-4 h-4 mr-2" /> Retake
            </Button>
            {!blurWarning && !lowLightWarning && (
              <>
                <Button onClick={() => setShowCropper(true)} variant="outline" size="lg" className="glass border-border text-foreground">
                  Crop
                </Button>
                <Button onClick={confirm} size="lg" className="gradient-cta text-primary-foreground font-semibold hover-scale btn-glow">
                  <Check className="w-4 h-4 mr-2" /> Use Photo
                </Button>
              </>
            )}
          </>
        )}
      </div>
    </motion.div>
  );
};

export default CameraCapture;
