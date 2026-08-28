"""BetterSnap generation-quality evaluation harness (Phase 1).

A STANDALONE, CPU-only research tool — NOT part of the GPU image or the Functions app.
It scores generated headshots against a user's accepted input set across FOUR SEPARATE
dimensions and never blends them into a single number until human-review data justifies
weights:

    identity          — face-embedding similarity vs a cleaned input centroid
    composition       — face size, count, gaze/yaw, centering, headroom (YuNet)
    prompt_adherence  — attire/background compliance  (interface only; needs a licensed
                        classifier — see report.py; deliberately NOT faked)
    visual_quality    — sharpness now; skin-realism is human-review only

Design constraints (from review): keep the groups separate; build the identity centroid
by cleaning outliers, not blind-averaging; derive thresholds from your OWN validation data
(a raw cosine value is not portable across models); and include BLINDED human review.
"""
