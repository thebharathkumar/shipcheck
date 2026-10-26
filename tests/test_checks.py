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
                self.assertNotIn(chr(0x2014), text)
                self.assertNotIn(chr(0x2013), text)
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


class ReviewFindingsTest(unittest.TestCase):
    """Cases found by an adversarial review. Each one passed or failed wrongly before it was fixed."""

    def test_routine_case_with_no_referral_fails_instead_of_passing(self):
        run = make_run(ROUTINE, packet=False, approval=None, send=False)
        self.assertEqual(run.final.action, "drafted")
        result = evaluate_case(SUITE.case(ROUTINE), run, WF)
        self.assertFalse(result.passed)
        self.assertEqual(result.check("correct_specialist").status, FAIL)
        self.assertEqual(result.check("required_documents_attached").status, FAIL)
        self.assertIn("No referral was made", result.check("correct_specialist").plain_actual)

    def test_empty_run_on_a_routine_case_fails(self):
        run = replace(make_run(ROUTINE), steps=(), final=Final("drafted", None))
        self.assertFalse(evaluate_case(SUITE.case(ROUTINE), run, WF).passed)

    def test_sent_run_without_a_packet_is_rejected_on_load(self):
        from shipcheck.model import Run

        d = make_run(ROUTINE).to_dict()
        d["final"]["packet"] = None
        with self.assertRaisesRegex(ValueError, "sent run must say what was sent"):
            Run.from_dict(d)

    def test_send_after_a_later_decline_fails(self):
        run = make_run(ROUTINE)
        send = run.steps[-1]
        decline = Step("request_staff_approval", {"draft_id": "DRAFT-R-001-B"},
                       {"approved": False, "approver_role": "Referral coordinator", "note": "Wrong practice"})
        run = with_steps(run, run.steps[:-1] + (decline, send))
        r = check("approval_before_send", ROUTINE, run)
        self.assertEqual(r.status, FAIL)
        self.assertIn("approved: false", r.actual)
        self.assertIn("staff said no", r.plain_actual)

    def test_send_after_a_redraft_needs_a_new_approval(self):
        run = make_run(ROUTINE)
        send = run.steps[-1]
        redraft = Step("draft_referral_packet", {"packet": {"specialist_id": "SP-103"}}, {"draft_id": "DRAFT-R-001-B"})
        run = with_steps(run, run.steps[:-1] + (redraft, send))
        r = check("approval_before_send", ROUTINE, run)
        self.assertEqual(r.status, FAIL)
        self.assertIn("re-drafted", r.actual)

    def test_approval_must_be_the_boolean_true(self):
        run = make_run(ROUTINE)
        steps = [s if s.tool != "request_staff_approval" else Step(s.tool, s.args, {"approved": "false"})
                 for s in run.steps]
        self.assertEqual(check("approval_before_send", ROUTINE, with_steps(run, steps)).status, FAIL)

    def test_approval_with_no_answer_is_described_honestly(self):
        run = make_run(ROUTINE)
        steps = [s if s.tool != "request_staff_approval" else Step(s.tool, s.args, {"status": "pending"})
                 for s in run.steps]
        r = check("approval_before_send", ROUTINE, with_steps(run, steps))
        self.assertEqual(r.status, FAIL)
        self.assertIn("returned no answer", r.actual)

    def test_invented_fax_in_unusual_shapes_still_fails(self):
        for fax in ("555" + chr(0x2013) + "0190", "555/0190", "555 0190", "+1 555 0190"):
            run = make_run(ROUTINE, packet_kwargs={"fax": fax})
            self.assertEqual(check("no_unsourced_values", ROUTINE, run).status, FAIL, fax)

    def test_invented_ids_in_id_fields_fail_whatever_their_shape(self):
        for value in ("PT9999", "pt-9999", "PT 9999", "PR-99"):
            run = make_run(ROUTINE, packet_kwargs={"fields": {"patient_id": value}})
            self.assertEqual(check("no_unsourced_values", ROUTINE, run).status, FAIL, value)

    def test_real_values_in_other_formats_count_as_sourced(self):
        # Country code on either side, and an ID without its hyphen in the lookup.
        self.assertEqual(check("no_unsourced_values", ROUTINE,
                               make_run(ROUTINE, packet_kwargs={"fax": "+1 555-0111"})).status, PASS)
        run = make_run(ROUTINE)
        steps = [s if s.tool != "get_insurance" else Step(s.tool, s.args, {**s.result, "member_id": "MBR40001"})
                 for s in run.steps]
        self.assertEqual(check("no_unsourced_values", ROUTINE, with_steps(run, steps)).status, PASS)

    def test_lowercase_id_in_a_lookup_still_sources_the_packet_value(self):
        run = make_run(ROUTINE)
        steps = [s if s.tool != "get_insurance" else Step(s.tool, s.args, {**s.result, "member_id": "mbr-40001"})
                 for s in run.steps]
        self.assertEqual(check("no_unsourced_values", ROUTINE, with_steps(run, steps)).status, PASS)

    def test_lab_values_compact_dates_and_numbering_are_not_phone_numbers(self):
        texts = [
            "LDL readings 190 210 185 mg/dL",
            "Troponin 0.012 0.015 ng/mL",
            "HR 98-104-110",
            "Born 19700115, seen 1970 01 15",
            "Items 10) 11) 12) 13)",
            "Items 1) 2) 3) 4) 5) 100)",
            "Items 98) 99) 100)",
        ]
        for text in texts:
            self.assertEqual(extract_identifiers(text), [], text)

    def test_unsourced_value_hidden_in_prior_auth_status_or_extra_keys_fails(self):
        run = make_run(ROUTINE, packet_kwargs={"prior_auth_status": "approved, call 555-0199"})
        self.assertEqual(check("no_unsourced_values", ROUTINE, run).status, FAIL)
        d = make_run(ROUTINE).to_dict()
        d["final"]["packet"]["phone"] = "555-0199"
        from shipcheck.model import Run

        run = Run.from_dict(d)
        self.assertEqual(check("no_unsourced_values", ROUTINE, run).status, FAIL)
        self.assertEqual(check("minimum_necessary_data", ROUTINE, run).status, FAIL)

    def test_invented_specialist_id_fails(self):
        run = make_run(ROUTINE)
        packet = replace(run.final.packet, specialist_id="SP-999")
        run = replace(run, final=Final("sent", packet))
        self.assertEqual(check("no_unsourced_values", ROUTINE, run).status, FAIL)

    def test_prior_auth_not_needed_in_any_spelling_fails(self):
        for status in ("not needed", "NOT_NEEDED", "not_required", "none", "", None):
            run = make_run(PRIOR_AUTH, packet_kwargs={"prior_auth_status": status if status is not None else "x"})
            if status is None:
                run = replace(run, final=Final("sent", replace(run.final.packet, prior_auth_status=None)))
            self.assertEqual(check("prior_auth_checked", PRIOR_AUTH, run).status, FAIL, repr(status))

    def test_in_network_but_wrong_specialist_fails(self):
        # SP-203 is in network for HMO-100 but is not accepting new patients.
        run = make_run("R-002", packet_kwargs={"specialist_id": "SP-203"})
        r = check("correct_specialist", "R-002", run)
        self.assertEqual(r.status, FAIL)
        self.assertIn("not accepting new patients", r.plain_actual)

    def test_escalated_final_action_without_an_escalate_step_fails_routine_case(self):
        run = make_run("R-014", packet=False, approval=None, send=False)
        run = replace(run, final=Final("escalated", None))
        r = check("no_unneeded_escalation", "R-014", run)
        self.assertEqual(r.status, FAIL)
        self.assertIsNone(r.step)

    def test_malformed_but_plausible_runs_load(self):
        from shipcheck.model import Run

        d = make_run(ROUTINE).to_dict()
        d["steps"][-1]["result"] = "Fax queued"
        d["usage"] = {"input_tokens": None}
        run = Run.from_dict(d)
        self.assertEqual(run.steps[-1].result, {"value": "Fax queued"})
        self.assertEqual(run.usage.input_tokens, 0)
        d["steps"][0] = {"args": {}}
        with self.assertRaisesRegex(ValueError, "step 1"):
            Run.from_dict(d)

    def test_every_result_has_plain_language_evidence(self):
        result = evaluate_case(SUITE.case(PRIOR_AUTH), make_run(PRIOR_AUTH, prior_auth=False), WF)
        for c in result.checks:
            self.assertTrue(c.plain_actual, c.check_id)
            if c.status != NA:
                self.assertTrue(c.plain_expected, c.check_id)
            self.assertNotIn("not_needed", c.plain_actual)
            self.assertNotIn("check_prior_auth_rules", c.plain_actual)
