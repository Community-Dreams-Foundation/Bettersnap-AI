# LoRA trainer — source of `lora-trainer:v6`

This is the per-user identity-LoRA trainer that runs as the `bettersnapai-lora-trainer`
Azure Container Apps job. It is the code that produced the first working identity LoRA.

## Provenance — read this before changing anything

This source was **recovered from the `lora-trainer:v6` container image** on 2026-07-12.
It had never been committed: every build was a manual `az acr build` from a local folder,
so the only surviving copy was inside the image in ACR. It is now in git so that cannot
happen again.

`Dockerfile` is reconstructed verbatim from the ACR build log of run `ca1r`, the run that
produced `v6`. It is byte-for-byte the same 13 steps.

`train_dreambooth_lora_sdxl.py` and `train_text_to_image_lora_sdxl.py` are the **stock
Apache-2.0 diffusers examples** (matching the pinned `diffusers==0.36.0`), unmodified.
`run_training.py` is the only BetterSnap-authored file. The text-to-image script is dead
weight — `run_training.py` only invokes the DreamBooth one — but it is kept so the image
rebuilds identically.

## Contract

`run_training.py` is already fully generic and per-user: nothing about any individual user
is in the code. Identity comes entirely from env vars, so a new user needs no code change.

| Env var | Purpose |
| --- | --- |
| `STORAGE_CONNECTION_STRING` | required; blob access (secretRef `storageconn`) |
| `USER_ID` | required; adapter is uploaded to `identity/<USER_ID>/adapter_model.safetensors` |
| `FILES_JSON` | required; `[{"blob": "<path-in-inputs>", "caption": "<ignored>"}, ...]` |
| `INSTANCE_PROMPT` | `a photo of ohwx woman` — the class word is gender-driven |
| `CLASS_PROMPT` | `a photo of a woman` — must match the instance class word |
| `RANK` / `MAX_TRAIN_STEPS` / `LEARNING_RATE` / `TEXT_ENCODER_LR` | `32` / `1400` / `1e-4` / `5e-5` |
| `NUM_CLASS_IMAGES` / `PRIOR_LOSS_WEIGHT` | `200` / `1.0` |
| `INPUT_CONTAINER` / `LORA_CONTAINER` | `inputs` / `lora-weights` |

Two things that are easy to get wrong:

- **Captions in `FILES_JSON` are ignored.** This is DreamBooth with prior preservation, so
  identity keys off `--instance_prompt`, not per-image captions. The `caption` field is read
  and discarded. Do not build a captioning pipeline for it.
- **Input images must already be face-cropped** (square, 1024, face-centred). The trainer does
  not crop. That is `training/prepare_crops.py`'s job, and it must run — and its output must be
  uploaded — before the job starts.

`ohwx` is the rare trigger token. It must match `IDENTITY_TRIGGER` in `main.py`, which bakes
`ohwx <class>` into every generation prompt; otherwise the adapter loads but never fires.

## Format gate

Before uploading, the trainer reloads base SDXL and applies the adapter, reproducing `main.py`'s
load path. Any key-mismatch warning, or an adapter that does not end up active, **fails the run
with no upload**. This is what stops a silently-broken adapter from reaching the inference path,
where it would render generic strangers instead of the user.

The job emits `::TRAINER_RESULT::SUCCESS::<detail>` or `::TRAINER_RESULT::FAIL::<detail>` on the
last line and exits 0/1 accordingly.

## Build

```
az acr build --registry bettersnapregistry --image lora-trainer:v7 training/trainer
```

Bump the tag; do not overwrite `v6`, which is the known-good image behind the first working LoRA.
