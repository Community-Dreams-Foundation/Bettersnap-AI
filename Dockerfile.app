# Thin app layer: everything (models, deps, training scripts) is already baked in v41.
# Only main.py changes during generation-code iteration, so rebuild JUST that layer for
# ~2-3 min builds instead of ~15. Bump the FROM tag when the base (models/deps) changes.
# v43 already has OpenCV baked, so this is a pure COPY layer → fast (~5-6 min, base pull only).
FROM bettersnapregistry-gta3hah3g3bpgrcn.azurecr.io/inference:v43
WORKDIR /app
COPY main.py .
