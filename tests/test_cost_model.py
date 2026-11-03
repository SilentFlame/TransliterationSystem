"""Cost model: egress math and GPU comparison line stay consistent."""

import unittest

from benchmarks.cost_model import Assumptions, build_report, scenario_egress_gb


class CostModelTests(unittest.TestCase):
    def test_mirrors_cut_egress_vs_direct(self):
        a = Assumptions(machines=5000, courts=200)
        direct = scenario_egress_gb(a, "direct_full")
        mirrors = scenario_egress_gb(a, "court_mirrors_full")
        self.assertAlmostEqual(direct / mirrors, 5000 / 200)

    def test_weights_only_smaller_than_full(self):
        a = Assumptions()
        self.assertLess(
            scenario_egress_gb(a, "direct_weights_only"),
            scenario_egress_gb(a, "direct_full"),
        )

    def test_chosen_cheaper_than_gpu_line(self):
        report = build_report(Assumptions())
        self.assertLess(
            report["chosen_design"]["monthly_central_usd"],
            report["rejected_gpu_line"]["usd_per_month_one_instance"],
        )
        self.assertIn("court_mirrors_full", report["scenarios"])


if __name__ == "__main__":
    unittest.main()
