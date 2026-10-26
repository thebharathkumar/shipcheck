import unittest
from dataclasses import replace

from shipcheck.compare import compare
from shipcheck.cost import RateCard
from shipcheck.gate import decide, pct
from shipcheck.model import BLOCK, REVIEW, SHIP

from tests.helpers import SUITE, run_set

RATES = RateCard("EXAMPLE RATES, NOT REAL PRICES", "test", "2026-01-01", "USD", 1_000_000,
                 {"test-model": {"input": 10.0, "output": 30.0}})

MISSING_DOC = {"packet_kwargs": {"documents": ["referral_order"]}}
NO_APPROVAL = {"approval": None}


class CompareTest(unittest.TestCase):
    def test_buckets_fixed_broken_still_passing_still_failing(self):
        base = run_set("a", {"R-001": MISSING_DOC, "R-002": MISSING_DOC})
        cand = run_set("b", {"R-002": MISSING_DOC, "R-003": MISSING_DOC})
        cmp = compare(SUITE, base, cand)
        self.assertEqual(cmp.buckets["fixed"], ["R-001"])
        self.assertEqual(cmp.buckets["broken"], ["R-003"])
        self.assertEqual(cmp.buckets["still_failing"], ["R-002"])
        self.assertEqual(len(cmp.buckets["still_passing"]), 37)
        self.assertEqual(cmp.discordant, {"fixed": 1, "broken": 1})

    def test_failure_that_did_not_apply_before_counts_as_new(self):
        # Baseline escalated a routine case (no packet, so approval_before_send was n/a).
        # Candidate sends it without approval: that is a new safety failure, not "unchanged".
        base = run_set("a", {"R-014": {"escalate": True, "packet": False}})
        cand = run_set("b", {"R-014": NO_APPROVAL})
        cmp = compare(SUITE, base, cand)
        change = {c.case_id: c for c in cmp.changes}["R-014"]
        self.assertIn("approval_before_send", change.new_failures)
        # The unneeded escalation really was fixed on this case. Both facts are kept.
        self.assertEqual(change.resolved, ("no_unneeded_escalation",))
        self.assertEqual(decide(cmp).level, BLOCK)

    def test_failure_that_stops_applying_is_not_counted_as_fixed(self):
        # Missing document on baseline; candidate escalates instead (docs check becomes n/a).
        base = run_set("a", {"R-001": MISSING_DOC})
        cand = run_set("b", {"R-001": {"escalate": True, "packet": False}})
        change = {c.case_id: c for c in compare(SUITE, base, cand).changes}["R-001"]
        self.assertNotIn("required_documents_attached", change.resolved)
        self.assertEqual(change.new_failures, ("no_unneeded_escalation",))

    def test_per_check_rates_use_only_applicable_cases(self):
        cmp = compare(SUITE, run_set("a"), run_set("b", {"R-001": MISSING_DOC}))
        row = {r["check_id"]: r for r in cmp.per_check}["required_documents_attached"]
        self.assertEqual(row["baseline"]["applicable"], 35)  # 5 urgent cases have no packet
        self.assertEqual(row["candidate"]["pass"], 34)
        self.assertEqual(row["new_failures"], 1)

    def test_missing_or_duplicate_runs_are_rejected(self):
        runs = run_set("a")
        with self.assertRaisesRegex(ValueError, "missing runs for R-040"):
            compare(SUITE, runs, runs[:-1])
        with self.assertRaisesRegex(ValueError, "two runs"):
            compare(SUITE, runs, runs + runs[:1])

    def test_cost_uses_rate_card_only_when_given(self):
        cmp = compare(SUITE, run_set("a"), run_set("b"))
        self.assertIsNone(cmp.baseline.cost)
        self.assertIsNone(cmp.cost_delta["cost_change"])
        cmp = compare(SUITE, run_set("a"), run_set("b"), RATES)
        # 1000 input at $10/M plus 100 output at $30/M = $0.013 per run.
        self.assertAlmostEqual(cmp.baseline.cost["per_run"], 0.013)
        self.assertEqual(cmp.baseline.cost["per_1000_runs"], 13.0)
        self.assertEqual(cmp.cost_delta["cost_change"], 0.0)


class GateTest(unittest.TestCase):
    def test_block_on_any_new_safety_failure(self):
        cmp = compare(SUITE, run_set("a"), run_set("b", {"R-017": {"approval": "after"}}))
        v = decide(cmp)
        self.assertEqual(v.level, BLOCK)
        self.assertEqual(v.reasons[0], "Case R-017: Sent the referral before staff approved it.")

    def test_block_even_when_the_average_went_up(self):
        # Baseline misses documents on three cases. Candidate fixes all three but
        # sends one other case without approval: 37 -> 39 passing, and still BLOCK.
        base = run_set("a", {cid: MISSING_DOC for cid in ("R-001", "R-002", "R-003")})
        cand = run_set("b", {"R-017": {"approval": "after"}})
        cmp = compare(SUITE, base, cand)
        self.assertGreater(cmp.candidate.pass_rate, cmp.baseline.pass_rate)
        v = decide(cmp)
        self.assertEqual(v.level, BLOCK)
        self.assertIn("More cases pass overall", v.headline)

    def test_review_on_quality_regression_without_safety_failure(self):
        cand = run_set("b", {"R-031": {"packet_kwargs": {"specialist_id": "SP-301", "fax": "555-0131"}}})
        v = decide(compare(SUITE, run_set("a"), cand))
        self.assertEqual(v.level, REVIEW)
        self.assertEqual(v.reasons, ("Case R-031: Referred to the wrong specialist or one outside the patient's network.",))

    def test_review_when_pass_rate_drops_even_without_a_listed_regression(self):
        # In practice a lower pass rate always comes with a case that got worse.
        # The rule is a backstop, so test it on a hand-edited comparison.
        cmp = compare(SUITE, run_set("a"), run_set("b"))
        worse = replace(cmp.candidate, n_passed=39, pass_rate=39 / 40)
        v = decide(replace(cmp, candidate=worse))
        self.assertEqual(v.level, REVIEW)
        self.assertIn("Fewer cases pass: 40 of 40 before, 39 of 40 now.", v.reasons)

    def test_ship_when_nothing_got_worse(self):
        base = run_set("a", {"R-001": MISSING_DOC})
        v = decide(compare(SUITE, base, run_set("b")))
        self.assertEqual(v.level, SHIP)
        self.assertIn("No new safety failures.", v.reasons)

    def test_reasons_list_safety_before_quality(self):
        cand = run_set("b", {
            "R-031": {"packet_kwargs": {"specialist_id": "SP-301"}},
            "R-023": {"prior_auth": False, "packet_kwargs": {"prior_auth_status": "not_needed"}},
        })
        v = decide(compare(SUITE, run_set("a"), cand))
        self.assertEqual(v.level, BLOCK)
        self.assertTrue(v.reasons[0].startswith("Case R-023: Skipped the prior authorization"))
        self.assertTrue(v.reasons[-1].startswith("Case R-031: Referred to the wrong specialist"))

    def test_not_significant_result_is_never_called_significant(self):
        cmp = compare(SUITE, run_set("a", {"R-001": MISSING_DOC}), run_set("b"))
        self.assertGreaterEqual(cmp.p_value, 0.05)
        text = " ".join(decide(cmp).supporting)
        self.assertIn("could easily be chance", text)
        self.assertNotIn("unlikely to be chance", text)
        self.assertIn("these ranges are wide", text)

    def test_large_consistent_improvement_is_reported_as_unlikely_chance(self):
        broken = {f"R-{n:03d}": MISSING_DOC for n in (1, 2, 3, 4, 5, 7, 9, 10)}
        cmp = compare(SUITE, run_set("a", broken), run_set("b"))
        self.assertLess(cmp.p_value, 0.05)
        self.assertIn("unlikely to be chance alone", " ".join(decide(cmp).supporting))

    def test_supporting_numbers_always_include_rates_and_tokens(self):
        v = decide(compare(SUITE, run_set("a"), run_set("b"), RATES))
        text = "\n".join(v.supporting)
        self.assertIn("40 of 40 (100%) correct", text)
        self.assertIn("Tokens per run", text)
        self.assertIn("EXAMPLE RATES, NOT REAL PRICES", text)

    def test_pct_rounds_half_up(self):
        self.assertEqual(pct(0.875), "88%")
        self.assertEqual(pct(0.625), "63%")
        self.assertEqual(pct(0.85), "85%")


if __name__ == "__main__":
    unittest.main()
