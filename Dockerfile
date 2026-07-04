FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    python3.11 \
    python3.11-dev \
    python3-pip \
    libgl1 \
    libglib2.0-0 \
    git \
    fonts-liberation \
    curl \
    gnupg \
    && curl https://packages.microsoft.com/keys/microsoft.asc | apt-key add - \
    && curl https://packages.microsoft.com/config/ubuntu/22.04/prod.list > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update \
    && ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc-dev \
    && rm -rf /var/lib/apt/lists/*

RUN update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1

RUN curl -sS https://bootstrap.pypa.io/get-pip.py | python3.11

WORKDIR /app

COPY requirements.txt .

RUN python3.11 -m pip install --no-cache-dir numpy==1.26.4 && \
    python3.11 -m pip install --no-cache-dir \
        torch==2.5.1 \
        torchvision==0.20.1 \
        torchaudio==2.5.1 \
        --index-url https://download.pytorch.org/whl/cu121 && \
    python3.11 -m pip install --no-cache-dir -r requirements.txt

# ── Bake SDXL base 1.0 + fp16-fix VAE into the image ────────────────────────
# NO Azure Files mount, NO runtime HF download: the model ships in the image so
# the A100 starts inference with zero network dependency (offline, reproducible).
# Both repos are UNGATED — no HF token needed. Revisions pinned to commit hashes
# so a rebuild can never silently pull a changed 'main'.
#
# Base repo: fetch ONLY the fp16 variant (*.fp16.safetensors) + configs/tokenizers
# (*.json/*.txt) — skips the fp32 + .bin duplicates to keep the image (and cold-start
# pull) small. The VAE fp16-fix repo is tiny, so take it whole.
ENV HF_HUB_ENABLE_HXET=0
RUN huggingface-cli download stabilityai/stable-diffusion-xl-base-1.0 \
        --revision 462165984030d82259a11f4367a4eed129e94a7b \
        --include "*.json" "*.txt" "*.fp16.safetensors" \
        --local-dir /models/sdxl-base && \
    huggingface-cli download madebyollin/sdxl-vae-fp16-fix \
        --revision 207b116dae70ace3637169f1ddd2434b91b3a8cd \
        --local-dir /models/sdxl-vae && \
    rm -rf /root/.cache/huggingface

COPY main.py .

CMD ["python3.11", "main.py"]