"""B1 fix: build_combos_global must order the attire x background product COVERAGE-FIRST, so a
short session (image_count < full product) still shows every selected attire and every selected
background when the count allows — never silently dropping a whole selected attire.

Before the fix the product was attire-major ([(a, b) for a in attires for b in backgrounds])
and the generation loop took combos[i % len(combos)], so the first image_count picks were all
the first attire and any later attire rendered ZERO images.

Pure functions (shared.catalog); CUDA-free, stdlib only — runs in CI.
Run: python -m unittest tests.test_combo_coverage
"""
import collections
import os
import sys
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from shared import catalog  # noqa: E402


def _valid_ref_pool():
    """Distinct valid attire/background refs pulled from the real catalog, for building grids
    of arbitrary shape in the permutation test."""
    attires, backgrounds = [], []
    for cat, data in catalog.CATEGORY_CATALOG.items():
        # Attires are gender-keyed now; the 'other' set gives a stable distinct pool.
        for aid in (data.get("attires", {}).get("other") or {}):
            attires.append(f"{cat}.{aid}")
        for bid in (data.get("backgrounds") or {}):
            backgrounds.append(f"{cat}.{bid}")
    return attires, backgrounds


ATTIRES = ["business_suit.navy_suit_tie", "business_suit.charcoal_suit_tie"]
BGS = [
    "business_suit.light_gray_studio", "business_suit.white_studio",
    "business_suit.modern_office", "business_suit.executive_office",
    "business_suit.corporate_lobby",
]


def _delivered(attires, backgrounds, image_count):
    """Exactly what the generation loop produces: build_combos_global + combos[i % len]."""
    combos = catalog.build_combos_global(attires, backgrounds)
    return [combos[i % len(combos)] for i in range(image_count)]


class BuildCombosCoverage(unittest.TestCase):
    def test_short_session_shows_every_selected_attire(self):
        # THE B1 case: 2 attires x 5 backgrounds on 4 images. BOTH attires must now appear.
        delivered = _delivered(ATTIRES, BGS, 4)
        self.assertEqual({a for a, _ in delivered}, set(ATTIRES))

    def test_all_selections_appear_when_count_allows(self):
        # 5 images >= 5 backgrounds (and >= 2 attires) -> every selection appears.
        delivered = _delivered(ATTIRES, BGS, 5)
        self.assertEqual({a for a, _ in delivered}, set(ATTIRES))
        self.assertEqual({b for _, b in delivered}, set(BGS))

    def test_monthly_pro_3x3_on_5_covers_all_attires_and_backgrounds(self):
        # The realistic in-product regression: 3x3 combos on a default 5-image session used to
        # drop the third attire.
        attires = ["business_suit.navy_suit_tie", "business_suit.charcoal_suit_tie",
                   "dating.crew_neck_sweater"]
        bgs = ["business_suit.light_gray_studio", "business_suit.modern_office",
               "beach_sunset.golden_hour_beach"]
        delivered = _delivered(attires, bgs, 5)
        self.assertEqual({a for a, _ in delivered}, set(attires))
        self.assertEqual({b for _, b in delivered}, set(bgs))

    def test_large_count_cycles_all_combos(self):
        # image_count >> product still cycles through every combo (unchanged behavior).
        delivered = _delivered(ATTIRES, BGS, 100)
        self.assertEqual(set(delivered), {(a, b) for a in ATTIRES for b in BGS})

    def test_deterministic(self):
        self.assertEqual(catalog.build_combos_global(ATTIRES, BGS),
                         catalog.build_combos_global(ATTIRES, BGS))

    def test_dedupe_and_invalid_refs_dropped(self):
        combos = catalog.build_combos_global(
            ["business_suit.navy_suit_tie", "business_suit.navy_suit_tie", "business_suit.NOPE"],
            ["business_suit.light_gray_studio", "business_suit.light_gray_studio"])
        self.assertEqual(combos, [("business_suit.navy_suit_tie", "business_suit.light_gray_studio")])

    def test_single_dimensions_and_empty(self):
        self.assertEqual(len(catalog.build_combos_global(ATTIRES[:1], BGS)), 5)   # 1 attire
        self.assertEqual(len(catalog.build_combos_global(ATTIRES, BGS[:1])), 2)   # 1 background
        self.assertEqual(catalog.build_combos_global([], BGS), [])
        self.assertEqual(catalog.build_combos_global(ATTIRES, []), [])
        self.assertEqual(catalog.build_combos_global(None, None), [])


class PermutationInvariant(unittest.TestCase):
    """The full ordering must be a PERMUTATION of the complete attire x background product:
    every unique (attire, background) pair appears EXACTLY ONCE — no combo dropped, none
    duplicated. Checked across several grid shapes, including gcd(n_a, n_b) > 1 (2x4, 4x6)
    where a naive single-diagonal walk would miss pairs."""

    SHAPES = [(1, 1), (1, 5), (5, 1), (2, 4), (4, 6), (3, 3), (5, 2), (2, 5), (7, 3)]

    def test_every_unique_combination_appears_exactly_once(self):
        pool_a, pool_b = _valid_ref_pool()
        for n_a, n_b in self.SHAPES:
            self.assertGreaterEqual(len(pool_a), n_a)
            self.assertGreaterEqual(len(pool_b), n_b)
            attires, backgrounds = pool_a[:n_a], pool_b[:n_b]
            combos = catalog.build_combos_global(attires, backgrounds)
            counts = collections.Counter(combos)
            expected = {(a, b) for a in attires for b in backgrounds}
            # exactly the full product...
            self.assertEqual(set(counts), expected, f"{n_a}x{n_b}: not the complete product")
            # ...each pair exactly once (this is what makes it a permutation)...
            self.assertTrue(all(c == 1 for c in counts.values()),
                            f"{n_a}x{n_b}: some combo appears more than once")
            # ...and total length == n_a * n_b.
            self.assertEqual(len(combos), n_a * n_b, f"{n_a}x{n_b}: wrong length")


if __name__ == "__main__":
    unittest.main(verbosity=2)
