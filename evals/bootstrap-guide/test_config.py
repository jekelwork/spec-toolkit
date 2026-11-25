import tomllib
import unittest
from pathlib import Path

CONFIG = Path(__file__).resolve().parent / "config.toml"


class ConfigTest(unittest.TestCase):
    def setUp(self):
        with CONFIG.open("rb") as handle:
            self.config = tomllib.load(handle)

    def test_pinned_model_is_set(self):
        model = self.config["model"]
        self.assertIsInstance(model, str)
        self.assertTrue(model.strip())

    def test_budget_cap_is_a_positive_number(self):
        cap = self.config["max_budget_usd"]
        self.assertIsInstance(cap, (int, float))
        self.assertNotIsInstance(cap, bool)
        self.assertGreater(cap, 0)

    def test_defaults(self):
        self.assertEqual(self.config["timeout_seconds"], 600)
        self.assertEqual(self.config["jobs"], 4)
        self.assertIn(self.config["launcher"], {"devvm", "local"})

    def test_runs_per_scenario_is_not_a_setting(self):
        # The default (5) lives in the scenario (data-model.md): `harness.DEFAULT_RUNS`.
        import harness

        self.assertNotIn("runs", self.config)
        self.assertEqual(harness.DEFAULT_RUNS, 5)


if __name__ == "__main__":
    unittest.main()
