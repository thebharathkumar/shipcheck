"""Deterministic fixture generator for the referral coordinator demo.

This is not an agent and it calls no model. It writes the trajectories that
three versions of a referral agent could plausibly produce, so shipcheck has
realistic runs to compare. Which case gets which behaviour is set by the
explicit case-id lists below, never by random numbers, so every result in the
demo traces back to one line in this file.

Token counts are estimated from the size of each step (about four characters
per token), so they are stable across runs but are not measurements.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from datetime import date, timedelta

from shipcheck.model import Case, Final, Packet, Run, Step, Suite, Usage, Workflow

VERSIONS = {
    "v1": {
        "model_label": "large-model",
        "description": "Current agent: large model, original prompt.",
        "prompt_tokens": 1300,
        "output_overhead": 90,
    },
    "v2": {
        "model_label": "small-model",
        "description": "Proposed: small model plus a new checklist prompt.",
        "prompt_tokens": 1320,
        "output_overhead": 50,
    },
    "v3": {
        "model_label": "small-model",
        "description": "Proposed: v2 plus a guardrail that forces the approval and prior-auth steps and uses directory values only.",
        "prompt_tokens": 1360,
        "output_overhead": 50,
    },
}

# v1 (large model, original prompt).
V1_MISSING_DOCUMENT = {
    "R-004": "ecg",
    "R-011": "imaging_report",
    "R-019": "medication_list",
    "R-036": "medication_list",
}
V1_ESCALATES_ROUTINE_CASE = ["R-014", "R-027"]

# v2 (small model, checklist prompt). The checklist fixes three of v1's four
# missing documents and both unneeded escalations. It still misses R-036.
V2_MISSING_DOCUMENT = {"R-036": "medication_list"}
# New problems in v2:
V2_SENDS_BEFORE_APPROVAL = ["R-017"]
V2_SKIPS_PRIOR_AUTH = ["R-008", "R-023"]  # both HMO plans, which require prior auth
V2_PICKS_FIRST_LISTED_SPECIALIST = ["R-031"]  # the first result is out of network for this plan
V2_INVENTED_FAX = {"R-031": "555-0190"}  # a number no lookup returned

# v3 is v2 plus a guardrail in code, not in the prompt. It forces approval
# before any send, forces the prior-auth lookup, and overwrites contact values
# with the directory's. It does not check network status, so v2's
# out-of-network pick on R-031 remains.

# Environment, not agent: staff put this case on hold, for every version.
STAFF_HOLDS = {"R-033": "Hold until updated labs arrive."}

APPROVER_ROLE = "Referral coordinator"


@dataclass(frozen=True)
class Behaviour:
    missing_document: str | None = None
    escalates_routine_case: bool = False
    sends_before_approval: bool = False
    skips_prior_auth: bool = False
    picks_first_listed_specialist: bool = False
    invented_fax: str | None = None


def behaviour(version: str, case_id: str) -> Behaviour:
    if version == "v1":
        return Behaviour(
            missing_document=V1_MISSING_DOCUMENT.get(case_id),
            escalates_routine_case=case_id in V1_ESCALATES_ROUTINE_CASE,
        )
    v2 = Behaviour(
        missing_document=V2_MISSING_DOCUMENT.get(case_id),
        sends_before_approval=case_id in V2_SENDS_BEFORE_APPROVAL,
        skips_prior_auth=case_id in V2_SKIPS_PRIOR_AUTH,
        picks_first_listed_specialist=case_id in V2_PICKS_FIRST_LISTED_SPECIALIST,
        invented_fax=V2_INVENTED_FAX.get(case_id),
    )
    if version == "v2":
        return v2
    if version == "v3":
        return replace(v2, sends_before_approval=False, skips_prior_auth=False, invented_fax=None)
    raise ValueError(f"unknown version {version!r}")


# The synthetic clinic record behind each case.


def _n(case: Case) -> int:
    return int(case.case_id.split("-")[1])


def record(case: Case) -> dict:
    n = _n(case)
    return {
        "order_id": f"ORD-{5000 + n}",
        "date_of_birth": f"19{45 + n % 50:02d}-{n % 12 + 1:02d}-{n * 7 % 28 + 1:02d}",
        "member_id": f"MBR-{40000 + n}",
        "ordering_provider_id": f"PR-{301 + n % 3}",
        "order_date": (date(2026, 9, 1) + timedelta(days=n - 1)).isoformat(),
    }


def _tokens(obj) -> int:
    return max(1, math.ceil(len(json.dumps(obj, sort_keys=True)) / 4))


def _usage(version: str, case: Case, steps: list[Step], final: Final) -> Usage:
    """One model call per step plus one to finish. Each call re-reads the prompt and all earlier steps."""
    cfg = VERSIONS[version]
    context = cfg["prompt_tokens"] + _tokens(case.to_dict())
    inp = out = 0
    for step in steps:
        inp += context
        out += _tokens({"tool": step.tool, "args": step.args}) + cfg["output_overhead"]
        context += _tokens(step.to_dict())
    inp += context
    out += _tokens(final.to_dict()) + cfg["output_overhead"]
    return Usage(input_tokens=inp, output_tokens=out)


def simulate_case(case: Case, workflow: Workflow, version: str) -> Run:
    b = behaviour(version, case.case_id)
    rec = record(case)
    steps: list[Step] = []

    def call(tool: str, args: dict, result: dict) -> Step:
        s = Step(tool, args, result)
        steps.append(s)
        return s

    order = call("get_referral_order", {"order_id": rec["order_id"]}, {
        "order_id": rec["order_id"],
        "patient_id": case.patient_id,
        "date_of_birth": rec["date_of_birth"],
        "specialty": case.specialty,
        "referral_reason": case.referral_reason,
        "urgency_flags": list(case.urgency_flags),
        "ordering_provider_id": rec["ordering_provider_id"],
        "order_date": rec["order_date"],
        "available_documents": list(case.expected.required_documents) + ["visit_notes"],
    }).result
    coverage = "inactive" if "coverage_inactive" in case.urgency_flags else "active"
    insurance = call("get_insurance", {"patient_id": case.patient_id}, {
        "plan_id": case.insurance_plan,
        "member_id": rec["member_id"],
        "coverage_status": coverage,
    }).result

    urgent = [f for f in case.urgency_flags if f in workflow.escalation_flags]
    if urgent or b.escalates_routine_case:
        flag = (urgent or list(case.urgency_flags))[0]
        reason = f"Urgent flag on the order: {flag}." if urgent else f"Flag on the order: {flag}. Asking staff to be safe."
        call("escalate_to_staff", {"case_id": case.case_id, "reason": reason}, {
            "ticket_id": f"ESC-{700 + _n(case)}",
            "queue": "Nurse review",
        })
        final = Final("escalated", None)
        return _run(version, case, steps, final)

    if b.skips_prior_auth:
        prior_auth_status = "not_needed"
    else:
        required = workflow.prior_auth_required(case.insurance_plan, case.specialty)
        rule = workflow.prior_auth_rules[case.insurance_plan]["rule_id"]
        call("check_prior_auth_rules", {"plan_id": case.insurance_plan, "specialty": case.specialty}, {
            "prior_auth_required": required,
            "rule_id": rule,
        })
        prior_auth_status = "required" if required else "not_needed"

    listing = [
        {
            "specialist_id": s["specialist_id"],
            "practice_name": s["practice_name"],
            "in_network": case.insurance_plan in s["in_network_plans"],
            "accepting_new_patients": s["accepting_new_patients"],
            "fax": s["fax"],
            "phone": s["phone"],
        }
        for s in workflow.specialist_directory
        if s["specialty"] == case.specialty
    ]
    call("search_specialist_directory", {"specialty": case.specialty, "plan_id": case.insurance_plan}, {"results": listing})
    if b.picks_first_listed_specialist:
        chosen = listing[0]
    else:
        chosen = next(s for s in listing if s["in_network"] and s["accepting_new_patients"])
    fax = b.invented_fax or chosen["fax"]

    documents = [d for d in case.expected.required_documents if d != b.missing_document]
    for doc in documents:
        call("attach_document", {"order_id": rec["order_id"], "document": doc}, {
            "attached": doc,
            "document_id": f"DOC-{_n(case):03d}-{doc}",
        })

    packet = Packet(
        specialist_id=chosen["specialist_id"],
        fax=fax,
        documents=tuple(documents),
        prior_auth_status=prior_auth_status,
        fields={
            "patient_id": order["patient_id"],
            "date_of_birth": order["date_of_birth"],
            "member_id": insurance["member_id"],
            "insurance_plan": insurance["plan_id"],
            "referral_reason": order["referral_reason"],
            "ordering_provider_id": order["ordering_provider_id"],
            "order_date": order["order_date"],
        },
    )
    draft_id = f"DRAFT-{case.case_id}"
    call("draft_referral_packet", {"packet": packet.to_dict()}, {"draft_id": draft_id, "status": "drafted"})

    hold = STAFF_HOLDS.get(case.case_id)
    approval = {"approved": hold is None, "approver_role": APPROVER_ROLE}
    if hold:
        approval["note"] = hold
    send_args = {"draft_id": draft_id, "specialist_id": chosen["specialist_id"], "fax": fax}
    send_result = {"status": "sent", "confirmation_id": f"FAX-{9000 + _n(case)}"}

    if b.sends_before_approval:
        call("send_to_specialist", send_args, send_result)
        call("request_staff_approval", {"draft_id": draft_id}, approval)
        action = "sent"
    else:
        call("request_staff_approval", {"draft_id": draft_id}, approval)
        if approval["approved"]:
            call("send_to_specialist", send_args, send_result)
            action = "sent"
        else:
            action = "drafted"
    return _run(version, case, steps, Final(action, packet))


def _run(version: str, case: Case, steps: list[Step], final: Final) -> Run:
    return Run(
        run_id=f"{version}-{case.case_id}",
        case_id=case.case_id,
        agent_version=version,
        model_label=VERSIONS[version]["model_label"],
        steps=tuple(steps),
        final=final,
        usage=_usage(version, case, steps, final),
    )


def simulate(suite: Suite, version: str) -> list[Run]:
    return [simulate_case(c, suite.workflow, version) for c in suite.cases]


def runs_file(suite: Suite, version: str) -> dict:
    """The JSON shape `shipcheck compare` reads: one file per agent version."""
    return {
        "simulated": True,
        "notice": "Simulated runs from shipcheck.simulate. No model was called.",
        "agent_version": version,
        "model_label": VERSIONS[version]["model_label"],
        "description": VERSIONS[version]["description"],
        "runs": [r.to_dict() for r in simulate(suite, version)],
    }
