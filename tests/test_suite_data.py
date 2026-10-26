import unittest
from pathlib import Path

from shipcheck.model import load_suite, validate_suite

ROOT = Path(__file__).resolve().parent.parent
SUITE = ROOT / "suites" / "referral_coordinator"


class SuiteDataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite = load_suite(SUITE)

    def test_forty_cases_load_with_unique_ids(self):
        ids = [c.case_id for c in self.suite.cases]
        self.assertEqual(len(ids), 40)
        self.assertEqual(len(set(ids)), 40)

    def test_suite_is_internally_consistent(self):
        self.assertEqual(validate_suite(self.suite), [])

    def test_every_expected_specialist_is_in_directory_and_in_network(self):
        wf = self.suite.workflow
        for c in self.suite.cases:
            sp = wf.specialist(c.expected.specialist_id)
            self.assertIsNotNone(sp, c.case_id)
            self.assertEqual(sp["specialty"], c.specialty, c.case_id)
            self.assertTrue(wf.in_network(sp["specialist_id"], c.insurance_plan), c.case_id)

    def test_about_six_cases_come_from_staff_flags_with_notes(self):
        flagged = [c for c in self.suite.cases if c.origin == "staff_flag"]
        self.assertEqual(len(flagged), 6)
        for c in flagged:
            self.assertTrue(c.origin_note.startswith("Staff flagged:"), c.case_id)

    def test_data_is_obviously_synthetic(self):
        wf = self.suite.workflow
        for c in self.suite.cases:
            self.assertRegex(c.patient_id, r"^PT-\d{4}$")
            self.assertRegex(c.case_id, r"^R-\d{3}$")
        numbers = [n for sp in wf.specialist_directory for n in (sp["fax"], sp["phone"])]
        self.assertEqual(len(numbers), len(set(numbers)), "directory numbers must be unique")
        for n in numbers:
            self.assertRegex(n, r"^555-01\d\d$")

    def test_validation_catches_an_out_of_network_expectation(self):
        from dataclasses import replace

        c = self.suite.case("R-031")
        bad = replace(c, expected=replace(c.expected, specialist_id="SP-301"))
        broken = replace(self.suite, cases=(bad,))
        problems = validate_suite(broken)
        self.assertTrue(any("not in network" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
