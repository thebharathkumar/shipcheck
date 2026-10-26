import json
import unittest

from shipcheck import simulate as sim
from shipcheck.compare import compare
from shipcheck.cost import load_rate_card
from shipcheck.gate import decide
from shipcheck.model import BLOCK, REVIEW, Run

from tests.helpers import ROOT, SUITE

RATES = load_rate_card(ROOT / "rates.example.json")


class SimulatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs = {v: sim.simulate(SUITE, v) for v in sim.VERSIONS}
        cls.v2 = compare(SUITE, cls.runs["v1"], cls.runs["v2"], RATES)
        cls.v3 = compare(SUITE, cls.runs["v1"], cls.runs["v3"], RATES)

    def test_output_is_identical_on_every_run(self):
        again = sim.simulate(SUITE, "v2")
        self.assertEqual([r.to_dict() for r in again], [r.to_dict() for r in self.runs["v2"]])

    def test_runs_round_trip_through_json(self):
        for run in self.runs["v3"]:
            self.assertEqual(Run.from_dict(json.loads(json.dumps(run.to_dict()))), run)

    def test_case_lists_point_at_real_cases(self):
        ids = {c.case_id for c in SUITE.cases}
        lists = [sim.V1_MISSING_DOCUMENT, sim.V1_ESCALATES_ROUTINE_CASE, sim.V2_MISSING_DOCUMENT,
                 sim.V2_SENDS_BEFORE_APPROVAL, sim.V2_SKIPS_PRIOR_AUTH, sim.V2_PICKS_FIRST_LISTED_SPECIALIST,
                 sim.V2_INVENTED_FAX, sim.STAFF_HOLDS]
        for lst in lists:
            self.assertTrue(set(lst) <= ids, lst)
        for cid in sim.V2_SKIPS_PRIOR_AUTH:
            self.assertTrue(SUITE.case(cid).insurance_plan.startswith("HMO"), cid)
        directory_numbers = {s["fax"] for s in SUITE.workflow.specialist_directory}
        self.assertTrue(set(sim.V2_INVENTED_FAX.values()).isdisjoint(directory_numbers))

    def test_v2_average_is_not_lower_than_v1_yet_it_is_blocked(self):
        self.assertGreaterEqual(self.v2.candidate.pass_rate, self.v2.baseline.pass_rate)
        self.assertEqual(decide(self.v2).level, BLOCK)

    def test_v2_introduces_exactly_the_scripted_safety_failures(self):
        new = sorted((ch.case_id, f) for ch in self.v2.changes for f in ch.new_failures)
        self.assertEqual(new, [
            ("R-008", "prior_auth_checked"),
            ("R-017", "approval_before_send"),
            ("R-023", "prior_auth_checked"),
            ("R-031", "correct_specialist"),
            ("R-031", "no_unsourced_values"),
        ])

    def test_v3_keeps_the_fixes_and_is_review(self):
        self.assertEqual(decide(self.v3).level, REVIEW)
        self.assertEqual(self.v3.buckets["fixed"], self.v2.buckets["fixed"])
        self.assertEqual(self.v3.buckets["broken"], ["R-031"])
        self.assertEqual(self.v3.candidate.safety_issues, 0)

    def test_small_model_is_much_cheaper_at_example_rates_with_similar_tokens(self):
        for cmp in (self.v2, self.v3):
            self.assertLess(abs(cmp.cost_delta["tokens_change"]), 0.10)
            self.assertLess(cmp.cost_delta["cost_change"], -0.6)

    def test_staff_hold_stops_at_draft_for_every_version(self):
        for v in sim.VERSIONS:
            run = {r.case_id: r for r in self.runs[v]}["R-033"]
            self.assertEqual(run.final.action, "drafted")
            self.assertNotIn("send_to_specialist", [s.tool for s in run.steps])


if __name__ == "__main__":
    unittest.main()
