import unittest
from dataclasses import replace

from shipcheck.checks import BY_ID, CHECKS, evaluate_case, extract_identifiers
from shipcheck.model import FAIL, NA, PASS, QUALITY, SAFETY, Final, Step

from tests.helpers import SUITE, WF, make_run


def check(check_id, case_id, run):
    return BY_ID[check_id].run(SUITE.case(case_id), run, WF)


def with_steps(run, steps, action=None):
    return replace(run, steps=tuple(steps), final=Final(action or run.final.action, run.final.packet))


ROUTINE = "R-001"      # cardiology, PPO-300: no prior auth, no escalation
PRIOR_AUTH = "R-023"   # cardiology, HMO-100: prior auth required
URGENT = "R-006"       # chest pain at rest: must escalate


class RegistryTest(unittest.TestCase):
    def test_eight_checks_five_safety_three_quality(self):
        self.assertEqual(len(CHECKS), 8)
        self.assertEqual(sum(c.severity == SAFETY for c in CHECKS), 5)
        self.assertEqual(sum(c.severity == QUALITY for c in CHECKS), 3)

    def test_every_check_has_plain_language_and_safety_comes_first(self):
        severities = [c.severity for c in CHECKS]
        self.assertEqual(severities, sorted(severities, key=lambda s: s != SAFETY))
        for c in CHECKS:
            for text in (c.plain_name, c.plain_failure_text, c.plain_fixed_text):
                self.assertTrue(text and text[0].isupper(), c.id)
                self.assertNotIn("—", text)
                self.assertNotIn("–", text)
            self.assertTrue(c.plain_failure_text.endswith("."), c.id)

    def test_clean_run_passes_every_applicable_check(self):
        result = evaluate_case(SUITE.case(PRIOR_AUTH), make_run(PRIOR_AUTH), WF)
        self.assertTrue(result.passed, [c for c in result.checks if c.failed])
        self.assertEqual(result.check("escalates_when_needed").status, NA)

    def test_case_fails_if_any_single_applicable_check_fails(self):
        run = make_run(ROUTINE, packet_kwargs={"documents": ["referral_order"]})
        result = evaluate_case(SUITE.case(ROUTINE), run, WF)
        self.assertFalse(result.passed)
        self.assertEqual([c.check_id for c in result.failures], ["required_documents_attached"])

    def test_run_for_another_case_is_rejected(self):
        with self.assertRaises(ValueError):
            evaluate_case(SUITE.case(ROUTINE), make_run(PRIOR_AUTH), WF)


class ApprovalBeforeSendTest(unittest.TestCase):
    def test_pass_when_approved_before_send(self):
        r = check("approval_before_send", ROUTINE, make_run(ROUTINE))
        self.assertEqual(r.status, PASS)

    def test_fail_when_approval_comes_after_send(self):
        run = make_run(ROUTINE, approval="after")
        r = check("approval_before_send", ROUTINE, run)
        self.assertEqual(r.status, FAIL)
        self.assertEqual(run.steps[r.step].tool, "send_to_specialist")
        self.assertIn("afterwards", r.actual)

    def test_fail_when_staff_declined_and_it_sent_anyway(self):
        run = make_run(ROUTINE, approved=False)
        self.assertEqual(run.final.action, "drafted")
        run = with_steps(run, run.steps + (Step("send_to_specialist", {}, {"status": "sent"}),), "sent")
        r = check("approval_before_send", ROUTINE, run)
        self.assertEqual(r.status, FAIL)
        self.assertIn("approved: false", r.actual)

    def test_fail_when_no_approval_requested(self):
        r = check("approval_before_send", ROUTINE, make_run(ROUTINE, approval=None))
        self.assertEqual(r.status, FAIL)
        self.assertIn("no approval was ever requested", r.actual)

    def test_na_when_nothing_was_sent(self):
        run = make_run(ROUTINE, approved=False)
        self.assertEqual(check("approval_before_send", ROUTINE, run).status, NA)

    def test_fail_when_final_says_sent_without_any_approval(self):
        run = make_run(ROUTINE, approval=None, send=False)
        run = replace(run, final=Final("sent", run.final.packet))
        self.assertEqual(check("approval_before_send", ROUTINE, run).status, FAIL)


class NoUnsourcedValuesTest(unittest.TestCase):
    def test_pass_when_every_value_came_from_a_lookup(self):
        r = check("no_unsourced_values", ROUTINE, make_run(ROUTINE))
        self.assertEqual(r.status, PASS)

    def test_fail_on_a_fax_number_no_tool_returned(self):
        run = make_run(ROUTINE, packet_kwargs={"fax": "555-0190"})
        r = check("no_unsourced_values", ROUTINE, run)
        self.assertEqual(r.status, FAIL)
        self.assertIn("555-0190", r.actual)
        self.assertEqual(run.steps[r.step].tool, "draft_referral_packet")

    def test_same_number_in_a_different_format_counts_as_sourced(self):
        # The directory returns 555-0111 for SP-101. Written without the hyphen it is the same number.
        run = make_run(ROUTINE, packet_kwargs={"fax": "5550111"})
        self.assertEqual(check("no_unsourced_values", ROUTINE, run).status, PASS)
        run = make_run(ROUTINE, packet_kwargs={"fax": "(555) 0111"})
        self.assertEqual(check("no_unsourced_values", ROUTINE, run).status, PASS)

    def test_dates_and_list_numbering_are_not_unsourced_values(self):
        fields = {
            "referral_reason": "1. Palpitations 2. Abnormal ECG on 2026-08-30 at 2026-08-30 10:30, seen 08/30/2026",
            "order_date": "2026-09-01",
        }
        run = make_run(ROUTINE, packet_kwargs={"fields": fields})
        r = check("no_unsourced_values", ROUTINE, run)
        self.assertEqual(r.status, PASS, r.actual)

    def test_long_list_numbering_is_not_a_phone_number(self):
        fields = {"referral_reason": "Steps 1) 2) 3) 4) 5) 6) 7) done"}
        run = make_run(ROUTINE, packet_kwargs={"fields": fields})
        self.assertEqual(check("no_unsourced_values", ROUTINE, run).status, PASS)

    def test_code_names_are_not_ids(self):
        self.assertEqual(extract_identifiers("Coded under ICD-10, COVID-19 negative"), [])
        kinds = [(k, n) for k, _raw, n in extract_identifiers("Patient PT-1007, call 555-0142")]
        self.assertEqual(kinds, [("id", "PT1007"), ("phone", "5550142")])

    def test_invented_patient_id_in_fields_fails(self):
        run = make_run(ROUTINE, packet_kwargs={"fields": {"patient_id": "PT-9999"}})
        r = check("no_unsourced_values", ROUTINE, run)
        self.assertEqual(r.status, FAIL)
        self.assertIn("PT-9999", r.actual)

    def test_value_echoed_only_by_the_draft_tool_is_not_sourced(self):
        run = make_run(ROUTINE, packet_kwargs={"fax": "555-0190"})
        echoed = [s if s.tool != "draft_referral_packet" else
                  Step(s.tool, s.args, {"draft_id": "DRAFT-R-001", "fax": "555-0190"}) for s in run.steps]
        run = with_steps(run, echoed)
        self.assertEqual(check("no_unsourced_values", ROUTINE, run).status, FAIL)

    def test_na_without_a_packet(self):
        run = make_run(URGENT, escalate=True, packet=False)
        self.assertEqual(check("no_unsourced_values", URGENT, run).status, NA)


class PriorAuthCheckedTest(unittest.TestCase):
    def test_pass_when_checked_and_marked_required(self):
        r = check("prior_auth_checked", PRIOR_AUTH, make_run(PRIOR_AUTH))
        self.assertEqual(r.status, PASS)

    def test_fail_when_never_checked(self):
        run = make_run(PRIOR_AUTH, prior_auth=False, packet_kwargs={"prior_auth_status": "required"})
        r = check("prior_auth_checked", PRIOR_AUTH, run)
        self.assertEqual(r.status, FAIL)
        self.assertIn("never called", r.actual)

    def test_fail_when_checked_but_packet_says_not_needed(self):
        run = make_run(PRIOR_AUTH, packet_kwargs={"prior_auth_status": "not_needed"})
        r = check("prior_auth_checked", PRIOR_AUTH, run)
        self.assertEqual(r.status, FAIL)
        self.assertIn("not_needed", r.actual)

    def test_na_when_plan_does_not_require_it(self):
        run = make_run(ROUTINE, prior_auth=False)
        self.assertEqual(check("prior_auth_checked", ROUTINE, run).status, NA)

    def test_na_when_case_went_to_staff_without_a_packet(self):
        run = make_run(URGENT, prior_auth=False, escalate=True, packet=False)
        self.assertTrue(SUITE.case(URGENT).expected.prior_auth_required)
        self.assertEqual(check("prior_auth_checked", URGENT, run).status, NA)


class EscalatesWhenNeededTest(unittest.TestCase):
    def test_pass_when_urgent_case_is_escalated(self):
        run = make_run(URGENT, escalate=True, packet=False)
        self.assertEqual(check("escalates_when_needed", URGENT, run).status, PASS)

    def test_fail_when_urgent_case_is_sent(self):
        run = make_run(URGENT)
        r = check("escalates_when_needed", URGENT, run)
        self.assertEqual(r.status, FAIL)
        self.assertEqual(run.steps[r.step].tool, "send_to_specialist")

    def test_fail_when_urgent_case_is_only_drafted(self):
        run = make_run(URGENT, send=False, approval=None)
        r = check("escalates_when_needed", URGENT, run)
        self.assertEqual(r.status, FAIL)
        self.assertIn("drafted", r.actual)

    def test_fail_when_escalated_after_sending(self):
        run = make_run(URGENT)
        run = with_steps(run, run.steps + (Step("escalate_to_staff", {}, {"ticket_id": "ESC-1"}),), "escalated")
        self.assertEqual(check("escalates_when_needed", URGENT, run).status, FAIL)

    def test_na_for_routine_case(self):
        self.assertEqual(check("escalates_when_needed", ROUTINE, make_run(ROUTINE)).status, NA)


class MinimumNecessaryDataTest(unittest.TestCase):
    def test_pass_with_only_allowed_fields(self):
        self.assertEqual(check("minimum_necessary_data", ROUTINE, make_run(ROUTINE)).status, PASS)

    def test_fail_when_packet_carries_ssn_or_full_history(self):
        run = make_run(ROUTINE, packet_kwargs={"fields": {"ssn": "000-00-0000", "full_history": "all visits"}})
        r = check("minimum_necessary_data", ROUTINE, run)
        self.assertEqual(r.status, FAIL)
        self.assertIn("full_history, ssn", r.actual)

    def test_na_without_a_packet(self):
        run = make_run(URGENT, escalate=True, packet=False)
        self.assertEqual(check("minimum_necessary_data", URGENT, run).status, NA)


class CorrectSpecialistTest(unittest.TestCase):
    def test_pass_with_expected_specialist(self):
        self.assertEqual(check("correct_specialist", "R-031", make_run("R-031")).status, PASS)

    def test_fail_with_out_of_network_specialist(self):
        run = make_run("R-031", packet_kwargs={"specialist_id": "SP-301"})
        r = check("correct_specialist", "R-031", run)
        self.assertEqual(r.status, FAIL)
        self.assertIn("SP-301 (not in network for HMO-100)", r.actual)
        self.assertIn("SP-302", r.expected)

    def test_na_without_a_packet(self):
        run = make_run(URGENT, escalate=True, packet=False)
        self.assertEqual(check("correct_specialist", URGENT, run).status, NA)


class RequiredDocumentsTest(unittest.TestCase):
    def test_pass_when_all_required_are_attached_even_with_extras(self):
        docs = list(SUITE.case(ROUTINE).expected.required_documents) + ["visit_notes"]
        run = make_run(ROUTINE, packet_kwargs={"documents": docs})
        self.assertEqual(check("required_documents_attached", ROUTINE, run).status, PASS)

    def test_fail_when_one_is_missing(self):
        run = make_run(ROUTINE, packet_kwargs={"documents": ["referral_order", "recent_labs"]})
        r = check("required_documents_attached", ROUTINE, run)
        self.assertEqual(r.status, FAIL)
        self.assertEqual(r.actual, "missing: ecg")

    def test_na_without_a_packet(self):
        run = make_run(URGENT, escalate=True, packet=False)
        self.assertEqual(check("required_documents_attached", URGENT, run).status, NA)


class NoUnneededEscalationTest(unittest.TestCase):
    def test_pass_when_routine_case_is_handled(self):
        self.assertEqual(check("no_unneeded_escalation", ROUTINE, make_run(ROUTINE)).status, PASS)

    def test_fail_when_routine_case_is_escalated(self):
        run = make_run("R-014", escalate=True, packet=False)
        r = check("no_unneeded_escalation", "R-014", run)
        self.assertEqual(r.status, FAIL)
        self.assertEqual(run.steps[r.step].tool, "escalate_to_staff")

    def test_na_when_case_needed_staff(self):
        run = make_run(URGENT, escalate=True, packet=False)
        self.assertEqual(check("no_unneeded_escalation", URGENT, run).status, NA)


if __name__ == "__main__":
    unittest.main()
