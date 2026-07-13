import { useRef, useCallback } from "react";
import Cropper, { ReactCropperElement } from "react-cropper";
import "cropperjs/dist/cropper.css";
import { Button } from "@/components/ui/button";
import { Check, RotateCcw } from "lucide-react";
import { motion } from "framer-motion";

interface ImageCropperProps {
  imageSrc: string;
  onCropComplete: (croppedFile: File) => void;
  onCancel: () => void;
  aspect?: number;
}

const ImageCropper = ({ imageSrc, onCropComplete, onCancel, aspect = 1 }: ImageCropperProps) => {
  const cropperRef = useRef<ReactCropperElement>(null);

  const handleConfirm = useCallback(() => {
    const cropper = cropperRef.current?.cropper;
    if (!cropper) return;

    cropper.getCroppedCanvas().toBlob(
      (blob) => {
        if (blob) {
          const file = new File([blob], `cropped-${Date.now()}.jpg`, { type: "image/jpeg" });
          onCropComplete(file);
        }
      },
      "image/jpeg",
      0.92
    );
  }, [onCropComplete]);

  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.95 }}
      animate={{ opacity: 1, scale: 1 }}
      className="glass rounded-2xl overflow-hidden shadow-glass"
    >
      <div className="w-full aspect-square bg-black/50">
        <Cropper
          ref={cropperRef}
          src={imageSrc}
          style={{ height: "100%", width: "100%" }}
          aspectRatio={aspect}
          viewMode={1}
          guides={true}
          background={false}
          responsive={true}
          autoCropArea={0.8}
          movable={true}
          zoomable={true}
          scalable={false}
          cropBoxMovable={true}
          cropBoxResizable={true}
          dragMode="move"
        />
      </div>

      <p className="text-xs text-muted-foreground text-center pt-3 px-4">
        Drag edges or corners to adjust crop. Drag inside to reposition.
      </p>

      <div className="flex justify-center gap-3 p-4">
        <Button
          onClick={onCancel}
          variant="outline"
          size="lg"
          className="glass border-border text-foreground"
        >
          <RotateCcw className="w-4 h-4 mr-2" /> Cancel
        </Button>
        <Button
          onClick={handleConfirm}
          size="lg"
          className="gradient-cta text-primary-foreground font-semibold hover-scale btn-glow"
        >
          <Check className="w-4 h-4 mr-2" /> Crop & Use
        </Button>
      </div>
    </motion.div>
  );
};

export default ImageCropper;
