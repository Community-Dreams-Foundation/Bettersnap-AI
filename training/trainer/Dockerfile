FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV HF_HUB_ENABLE_HF_TRANSFER=0
ENV PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3.11 python3.11-dev python3-pip git curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN update-alternatives --install /usr/bin/python  python  /usr/bin/python3.11 1 && \
    update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 && \
    python3.11 -m pip install --upgrade pip

RUN python3.11 -m pip install \
      torch==2.5.1 torchvision==0.20.1 --index-url https://download.pytorch.org/whl/cu121

RUN python3.11 -m pip install \
      diffusers==0.36.0 transformers==4.49.0 accelerate==1.14.0 peft==0.19.1 \
      safetensors "huggingface_hub>=0.34.0" Pillow azure-storage-blob \
      datasets

WORKDIR /workspace

COPY train_dreambooth_lora_sdxl.py /workspace/train_dreambooth_lora_sdxl.py
COPY train_text_to_image_lora_sdxl.py /workspace/train_text_to_image_lora_sdxl.py
COPY run_training.py /workspace/run_training.py

CMD ["python3.11", "run_training.py"]
