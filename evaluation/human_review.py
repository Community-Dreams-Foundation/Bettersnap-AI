"""Blinded human-review sheet builder.

Automated scores cannot judge whether an image truly looks like the person, whether skin
looks artificial, whether attire is professionally appropriate, or whether it is usable as
a LinkedIn headshot. Those need humans — and reviewers must NOT know which pipeline produced
an image, or the comparison is biased.

`build_blinded_review` shuffles images from all pipelines into one sheet with opaque review
ids, hiding pipeline/dataset. The separate `key` unblinds after scoring. Deterministic via
`seed` so a review is reproducible.
"""
import random


def build_blinded_review(items, seed):
    """items: list of dicts, each with at least 'image_path' plus provenance
    ('pipeline_id', 'dataset_id', ...). Returns {'sheet', 'key'}:
        sheet: [{review_id, image_path}]           <- given to reviewers (blinded)
        key:   {review_id: <full item incl provenance>}  <- for unblinding after scoring
    """
    rng = random.Random(seed)
    shuffled = list(items)
    rng.shuffle(shuffled)
    sheet, key = [], {}
    for i, it in enumerate(shuffled):
        if "image_path" not in it:
            raise ValueError("each review item needs an 'image_path'")
        rid = f"R{i:04d}"
        sheet.append({"review_id": rid, "image_path": it["image_path"]})
        key[rid] = dict(it)
    return {"sheet": sheet, "key": key}
