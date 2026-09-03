"""Lossless unit conversions at personal-plan boundaries.

One-time balances count images (1 unit per image). Monthly balances count credits, and
the active monthly plan defines how many credits buy one image. Persistent add-ons use
the same unit as the account they currently belong to.
"""


def images_to_credits(images: int, credits_per_image: int) -> int:
    if images < 0 or credits_per_image <= 0:
        raise ValueError("balances and conversion rates must be positive")
    return images * credits_per_image


def credits_to_images(credits: int, credits_per_image: int) -> int:
    if credits < 0 or credits_per_image <= 0:
        raise ValueError("balances and conversion rates must be positive")
    images, remainder = divmod(credits, credits_per_image)
    if remainder:
        raise ValueError(
            f"{credits} credits cannot be converted exactly at {credits_per_image} credits/image"
        )
    return images


# The one-time keys a downgrade may land on. "trial" is included because a free-trial
# account that subscribed and then cancelled never bought a pack: mapping it to "basic"
# handed it a PAID plan key it never purchased, so it read as a paying customer in the
# billing UI and in analytics. Both plans price at 1 credit/image, so nothing else moves.
_ONE_TIME_KEYS = {"trial", "basic", "pro", "expert"}


def one_time_key_for_monthly(plan_key: str | None) -> str:
    key = (plan_key or "").strip().lower()
    if key.startswith("monthly_"):
        key = key[len("monthly_"):]
    return key if key in _ONE_TIME_KEYS else "basic"
