import unittest
from app.security import clamp_limit, clean_search, positive_id, choice, ALLOWED_SALES_STATES

class SecurityTests(unittest.TestCase):
    def test_limit(self):
        self.assertEqual(clamp_limit(-1, 50), 1)
        self.assertEqual(clamp_limit(999, 50), 50)
    def test_search(self):
        self.assertEqual(clean_search(" test "), "test")
        self.assertEqual(len(clean_search("x" * 500)), 200)
    def test_id(self):
        self.assertEqual(positive_id(1, "id"), 1)
        with self.assertRaises(ValueError):
            positive_id(0, "id")
    def test_choice(self):
        self.assertEqual(choice("SALE", ALLOWED_SALES_STATES, "state"), "sale")
        with self.assertRaises(ValueError):
            choice("bad", ALLOWED_SALES_STATES, "state")

if __name__ == "__main__":
    unittest.main()
