"""Composition prompt steering (Phase 5) — pure string assembly, no torch.

Prompting alone does NOT guarantee composition (that is why output gates + regeneration
exist in Phase 4), but explicit headshot framing plus composition negatives measurably help.
Kept in its own module so main.py can apply it and tests can import the exact function.

DEFAULT OFF: the GPU worker only applies this when COMPOSITION_CONTROL=1, so the Phase-2
baseline (current prompts) stays a true control. The Phase-5 experiment turns it on.
"""

# Appended to the positive prompt tail. Explicit head-and-shoulders framing + eye contact.
FRAMING_POSITIVE = (
    "tight head-and-shoulders corporate portrait, face centered and occupying a large "
    "portion of the frame, camera at eye level, looking directly into the lens, "
    "both eyes clearly visible"
)

# Appended to the negative prompt. Steers away from the full-body / averted / cinematic
# compositions observed in the celebrity run.
FRAMING_NEGATIVE = (
    "full body, three-quarter body, wide shot, distant subject, small face, "
    "profile view, looking away, turned head, cropped face, cinematic scene"
)


def apply_composition_control(tail: str, negative: str, enabled: bool):
    """Return (tail, negative) with the framing phrase / composition negatives appended when
    enabled; unchanged otherwise. Idempotent-safe: does not append twice."""
    if not enabled:
        return tail, negative
    if FRAMING_POSITIVE not in tail:
        tail = f"{tail.rstrip().rstrip('.')}. {FRAMING_POSITIVE}." if tail else FRAMING_POSITIVE + "."
    if FRAMING_NEGATIVE not in (negative or ""):
        negative = f"{negative}, {FRAMING_NEGATIVE}" if negative else FRAMING_NEGATIVE
    return tail, negative
