import json
import re
import unittest
from dataclasses import replace

from shipcheck.cost import load_rate_card
from shipcheck.report import build_results, render_html
from shipcheck.simulate import VERSIONS, simulate

from tests.helpers import ROOT, SUITE

LABEL = "EXAMPLE RATES, NOT REAL PRICES"


class ReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs = {v: simulate(SUITE, v) for v in VERSIONS}
        cls.doc = build_results(SUITE, cls.runs, [("v1", "v2"), ("v1", "v3")],
                                load_rate_card(ROOT / "rates.example.json"),
                                {v: c["description"] for v, c in VERSIONS.items()})
        cls.html = render_html(cls.doc, cls.runs)
        cls.body = cls.html.split('<script type="application/json"')[0]

    def test_no_network_resource_loads(self):
        self.assertNotIn("http://", self.html)
        self.assertNotIn("https://", self.html)
        self.assertNotRegex(self.html, r"<script[^>]+src=")
        self.assertNotRegex(self.html, r"<link[^>]+stylesheet")
        self.assertNotIn("@import", self.html)
        self.assertNotRegex(self.html, r"url\(")

    def test_embedded_data_is_the_results_document(self):
        m = re.search(r'<script type="application/json" id="shipcheck-data">(.*?)</script>', self.html, re.S)
        embedded = json.loads(m.group(1))
        self.assertEqual(embedded["doc"], json.loads(json.dumps(self.doc)))
        self.assertEqual(set(embedded["runs"]), {"v1", "v2", "v3"})

    def test_verdicts_and_top_reasons_are_in_the_page(self):
        self.assertIn('<span class="badge">BLOCK</span>', self.body)
        self.assertIn('<span class="badge">REVIEW</span>', self.body)
        self.assertIn("Case R-017: Sent the referral before staff approved it.", self.body)
        self.assertIn("34 of 40 (85%)", self.body)
        self.assertIn("likely between 71% and 93%", self.body)

    def test_every_dollar_figure_sits_next_to_the_example_rates_label(self):
        chunks = re.split(r"(?=<section|<article)", self.body)
        priced = [c for c in chunks if re.search(r"\$\d", c)]
        self.assertTrue(priced)
        for chunk in priced:
            self.assertIn(LABEL, chunk)
        self.assertIn("Source: Invented for this demo", self.body)
        self.assertIn("Date: 2026-10-26", self.body)

    def test_page_says_data_is_simulated_and_approval_is_a_command(self):
        self.assertIn("All cases and agent runs are synthetic and simulated. No real patient data. "
                      "Example rates are not real prices.", self.body)
        self.assertIn("This page is a static file and cannot record anything.", self.body)
        self.assertIn('shipcheck approve --candidate v3 --by &quot;Clinic Ops Lead&quot; --note &quot;Reviewed R-031&quot;',
                      self.body)

    def test_blocked_change_has_no_approve_command(self):
        v2 = self.doc["comparisons"][0]
        self.assertIsNone(v2["commands"]["approve"])
        self.assertIn("shipcheck rollback --candidate v2", v2["commands"]["rollback"])

    def test_no_em_or_en_dashes_in_the_page(self):
        self.assertNotIn(chr(0x2014), self.html)
        self.assertNotIn(chr(0x2013), self.html)


if __name__ == "__main__":
    unittest.main()


class ReportReviewFindingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs = {v: simulate(SUITE, v) for v in VERSIONS}
        cls.rates = load_rate_card(ROOT / "rates.example.json")

    def page(self, pairs=(("v1", "v2"), ("v1", "v3")), rates="default", simulated=True, runs=None):
        runs = runs or self.runs
        doc = build_results(SUITE, runs, list(pairs), self.rates if rates == "default" else rates, simulated=simulated)
        return doc, render_html(doc, runs).split('<script type="application/json"')[0]

    def test_wide_interval_warning_is_visible(self):
        _, body = self.page()
        self.assertIn("With 40 cases these ranges are wide.", body)

    def test_manager_view_has_no_raw_enums_or_tool_names(self):
        _, body = self.page()
        for panel in re.findall(r'data-view="manager" role="tabpanel".*?(?=<div class="view" data-view="engineer")', body, re.S):
            for raw in ("not_needed", "check_prior_auth_rules", "approved: true", "medication_list", "referral_order"):
                self.assertNotIn(raw, panel)
        self.assertIn("It sent the packet at step 9. Staff were only asked afterwards, at step 10.", body)
        self.assertIn("It never checked prior authorization. The packet says it is not needed.", body)

    def test_missing_model_in_rate_card_is_named(self):
        partial = replace(self.rates, models={"large-model": self.rates.models["large-model"]})
        _, body = self.page(rates=partial)
        self.assertIn("The rate card has no rate for small-model", body)
        self.assertNotIn("No rate card given", body)

    def test_synthetic_suite_is_labelled_even_when_runs_are_not_marked_simulated(self):
        doc, body = self.page(simulated=False)
        self.assertIn("Synthetic test cases.", body)
        self.assertIn("The test cases are synthetic. No real patient data.", doc["notice"])

    def test_ship_banner_does_not_say_safe(self):
        runs = dict(self.runs, v1b=[replace(r, agent_version="v1b") for r in self.runs["v1"]])
        _, body = self.page(pairs=[("v1", "v1b")], runs=runs)
        self.assertIn("No regressions found", body)
        self.assertNotIn("Safe to ship", body)

    def test_got_better_and_got_worse_cover_every_flipped_case(self):
        doc, body = self.page()
        for cmp in doc["comparisons"]:
            for cid in cmp["buckets"]["fixed"] + cmp["buckets"]["broken"]:
                self.assertIn(f"<strong>Case {cid}:</strong>", body)
