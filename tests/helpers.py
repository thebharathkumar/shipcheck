"""Hand-built trajectories for tests. Independent of shipcheck.simulate on purpose."""

from __future__ import annotations

from pathlib import Path

from shipcheck.model import Final, Packet, Run, Step, Usage, load_suite

ROOT = Path(__file__).resolve().parent.parent
SUITE = load_suite(ROOT / "suites" / "referral_coordinator")
WF = SUITE.workflow


def lookups(case, *, prior_auth=True, coverage="active"):
    n = int(case.case_id.split("-")[1])
    steps = [
        Step("get_referral_order", {"order_id": f"ORD-{5000 + n}"}, {
            "order_id": f"ORD-{5000 + n}",
            "patient_id": case.patient_id,
            "date_of_birth": "1970-01-15",
            "specialty": case.specialty,
            "referral_reason": case.referral_reason,
            "ordering_provider_id": "PR-301",
            "order_date": "2026-09-01",
            "urgency_flags": list(case.urgency_flags),
        }),
        Step("get_insurance", {"patient_id": case.patient_id}, {
            "plan_id": case.insurance_plan,
            "member_id": f"MBR-{40000 + n}",
            "coverage_status": coverage,
        }),
    ]
    if prior_auth:
        steps.append(Step("check_prior_auth_rules", {"plan_id": case.insurance_plan, "specialty": case.specialty}, {
            "prior_auth_required": case.expected.prior_auth_required,
        }))
    results = [
        {
            "specialist_id": s["specialist_id"],
            "fax": s["fax"],
            "phone": s["phone"],
            "in_network": case.insurance_plan in s["in_network_plans"],
            "accepting_new_patients": s["accepting_new_patients"],
        }
        for s in WF.specialist_directory
        if s["specialty"] == case.specialty
    ]
    steps.append(Step("search_specialist_directory", {"specialty": case.specialty, "plan_id": case.insurance_plan},
                      {"results": results}))
    return steps


def packet_for(case, *, specialist_id=None, fax=None, documents=None, prior_auth_status=None, fields=None):
    n = int(case.case_id.split("-")[1])
    sid = specialist_id or case.expected.specialist_id
    sp = WF.specialist(sid)
    base_fields = {
        "patient_id": case.patient_id,
        "date_of_birth": "1970-01-15",
        "member_id": f"MBR-{40000 + n}",
        "insurance_plan": case.insurance_plan,
        "referral_reason": case.referral_reason,
        "ordering_provider_id": "PR-301",
        "order_date": "2026-09-01",
    }
    if fields:
        base_fields.update(fields)
    if prior_auth_status is None:
        prior_auth_status = "required" if case.expected.prior_auth_required else "not_needed"
    return Packet(
        specialist_id=sid,
        fax=fax if fax is not None else sp["fax"],
        documents=tuple(documents if documents is not None else case.expected.required_documents),
        prior_auth_status=prior_auth_status,
        fields=base_fields,
    )


def make_run(case_id, *, version="t", prior_auth=True, packet_kwargs=None, approval="before",
             approved=True, send=True, escalate=False, packet=True, coverage="active", extra_steps=()):
    """Build a run for a suite case.

    approval: "before" (approve then send), "after" (send then approve) or None (never ask).
    """
    case = SUITE.case(case_id)
    steps = lookups(case, prior_auth=prior_auth, coverage=coverage)
    pk = packet_for(case, **(packet_kwargs or {})) if packet else None
    if escalate:
        steps.append(Step("escalate_to_staff", {"case_id": case_id, "reason": "needs a person"}, {"ticket_id": "ESC-900"}))
    if pk is not None:
        for d in pk.documents:
            steps.append(Step("attach_document", {"document": d}, {"attached": d}))
        steps.append(Step("draft_referral_packet", {"packet": pk.to_dict()}, {"draft_id": f"DRAFT-{case_id}"}))
    steps.extend(extra_steps)
    approval_step = Step("request_staff_approval", {"draft_id": f"DRAFT-{case_id}"},
                         {"approved": approved, "approver_role": "Referral coordinator"})
    send_step = Step("send_to_specialist", {"draft_id": f"DRAFT-{case_id}"}, {"status": "sent"})
    if pk is not None and not escalate:
        if approval == "before":
            steps.append(approval_step)
        if send and (approved or approval != "before"):
            steps.append(send_step)
        if approval == "after":
            steps.append(approval_step)
    sent = any(s.tool == "send_to_specialist" for s in steps)
    action = "escalated" if escalate else ("sent" if sent else "drafted")
    return Run(f"{version}-{case_id}", case_id, version, "test-model", tuple(steps), Final(action, pk), Usage(1000, 100))


def run_set(version, overrides=None):
    """One clean run per suite case, with per-case keyword overrides for make_run."""
    overrides = overrides or {}
    runs = []
    for c in SUITE.cases:
        kw = dict(overrides.get(c.case_id, {}))
        if c.expected.must_escalate and "escalate" not in kw:
            kw.update(escalate=True, packet=False)
        runs.append(make_run(c.case_id, version=version, **kw))
    return runs
