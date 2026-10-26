import json
import re
import unittest

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
        self.assertNotIn("—", self.html)
        self.assertNotIn("–", self.html)


if __name__ == "__main__":
    unittest.main()
