import unittest

from shared.credit_units import credits_to_images, images_to_credits, one_time_key_for_monthly


class CreditUnitConversionTests(unittest.TestCase):
    def test_one_time_images_become_monthly_add_on_credits(self):
        self.assertEqual(images_to_credits(20, 5), 100)

    def test_monthly_add_on_credits_become_one_time_images(self):
        self.assertEqual(credits_to_images(100, 5), 20)

    def test_non_divisible_balance_fails_closed(self):
        with self.assertRaises(ValueError):
            credits_to_images(102, 5)

    def test_monthly_tier_maps_to_same_one_time_tier(self):
        self.assertEqual(one_time_key_for_monthly("monthly_pro"), "pro")


if __name__ == "__main__":
    unittest.main()
