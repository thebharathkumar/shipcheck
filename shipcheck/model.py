"""Plain data types shared by every shipcheck module.

Suites, runs and results are plain JSON on disk. Each dataclass here has a
from_dict and to_dict so the JSON stays the source of truth and nothing
depends on pickling or a database.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PASS = "pass"
FAIL = "fail"
NA = "n/a"

SAFETY = "safety"
QUALITY = "quality"

SHIP = "SHIP"
REVIEW = "REVIEW"
BLOCK = "BLOCK"

FINAL_ACTIONS = ("sent", "drafted", "escalated")


# Suite: what the agent is supposed to do


@dataclass(frozen=True)
class Expected:
    specialist_id: str
    prior_auth_required: bool
    must_escalate: bool
    required_documents: tuple[str, ...]

    @classmethod
    def from_dict(cls, d: dict) -> "Expected":
        return cls(
            specialist_id=d["specialist_id"],
            prior_auth_required=bool(d["prior_auth_required"]),
            must_escalate=bool(d["must_escalate"]),
            required_documents=tuple(d["required_documents"]),
        )

    def to_dict(self) -> dict:
        return {
            "specialist_id": self.specialist_id,
            "prior_auth_required": self.prior_auth_required,
            "must_escalate": self.must_escalate,
            "required_documents": list(self.required_documents),
        }


@dataclass(frozen=True)
class Case:
    case_id: str
    patient_id: str
    specialty: str
    insurance_plan: str
    referral_reason: str
    urgency_flags: tuple[str, ...]
    origin: str
    expected: Expected
    origin_note: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Case":
        origin = d.get("origin", "seed")
        if origin not in ("seed", "staff_flag"):
            raise ValueError(f"case {d.get('case_id')}: origin must be 'seed' or 'staff_flag', got {origin!r}")
        return cls(
            case_id=d["case_id"],
            patient_id=d["patient_id"],
            specialty=d["specialty"],
            insurance_plan=d["insurance_plan"],
            referral_reason=d["referral_reason"],
            urgency_flags=tuple(d.get("urgency_flags", [])),
            origin=origin,
            expected=Expected.from_dict(d["expected"]),
            origin_note=d.get("origin_note"),
        )

    def to_dict(self) -> dict:
        d = {
            "case_id": self.case_id,
            "patient_id": self.patient_id,
            "specialty": self.specialty,
            "insurance_plan": self.insurance_plan,
            "referral_reason": self.referral_reason,
            "urgency_flags": list(self.urgency_flags),
            "origin": self.origin,
        }
        if self.origin_note:
            d["origin_note"] = self.origin_note
        d["expected"] = self.expected.to_dict()
        return d


@dataclass(frozen=True)
class Workflow:
    name: str
    title: str
    tools: tuple[dict, ...]
    allowed_packet_fields: tuple[str, ...]
    required_documents: dict[str, tuple[str, ...]]
    prior_auth_rules: dict[str, dict]
    escalation_flags: tuple[str, ...]
    routine_flags: tuple[str, ...]
    specialist_directory: tuple[dict, ...]
    specialist_selection_rule: str = ""
    document_labels: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Workflow":
        return cls(
            name=d["name"],
            title=d.get("title", d["name"]),
            tools=tuple(d["tools"]),
            allowed_packet_fields=tuple(d["allowed_packet_fields"]),
            required_documents={k: tuple(v) for k, v in d["required_documents"].items()},
            prior_auth_rules=dict(d["prior_auth_rules"]),
            escalation_flags=tuple(d.get("escalation_flags", [])),
            routine_flags=tuple(d.get("routine_flags", [])),
            specialist_directory=tuple(d["specialist_directory"]),
            specialist_selection_rule=d.get("specialist_selection_rule", ""),
            document_labels=dict(d.get("document_labels", {})),
        )

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(t["name"] for t in self.tools)

    @property
    def lookup_tools(self) -> frozenset[str]:
        """Tools whose results count as a real source of phone, fax and ID values."""
        return frozenset(t["name"] for t in self.tools if t.get("kind") == "lookup")

    def specialist(self, specialist_id: str | None) -> dict | None:
        for s in self.specialist_directory:
            if s["specialist_id"] == specialist_id:
                return s
        return None

    def in_network(self, specialist_id: str | None, plan: str) -> bool:
        s = self.specialist(specialist_id)
        return bool(s) and plan in s["in_network_plans"]

    def doc_label(self, document: str) -> str:
        return self.document_labels.get(document, document.replace("_", " "))

    def prior_auth_required(self, plan: str, specialty: str) -> bool:
        rule = self.prior_auth_rules.get(plan, {})
        return specialty in rule.get("requires_prior_auth_for", [])


@dataclass(frozen=True)
class Suite:
    name: str
    workflow: Workflow
    cases: tuple[Case, ...]
    synthetic: bool = False

    def case(self, case_id: str) -> Case:
        for c in self.cases:
            if c.case_id == case_id:
                return c
        raise KeyError(case_id)


# Runs: what an agent actually did


def _as_dict(value) -> dict:
    """Tool args and results are usually objects. Keep anything else under "value"."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    return {"value": value}


@dataclass(frozen=True)
class Step:
    tool: str
    args: dict
    result: dict

    @classmethod
    def from_dict(cls, d: dict) -> "Step":
        if not isinstance(d, dict) or not d.get("tool"):
            raise ValueError("every step needs a 'tool' name")
        return cls(tool=str(d["tool"]), args=_as_dict(d.get("args")), result=_as_dict(d.get("result")))

    def to_dict(self) -> dict:
        return {"tool": self.tool, "args": self.args, "result": self.result}


PACKET_KEYS = ("specialist_id", "fax", "documents", "prior_auth_status", "fields")


@dataclass(frozen=True)
class Packet:
    specialist_id: str | None
    fax: str | None
    documents: tuple[str, ...]
    prior_auth_status: str | None
    fields: dict
    # Top-level keys outside the schema. Kept, not dropped, so the data
    # checks can see them: an "ssn" next to "fields" still counts as shared.
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Packet":
        if not isinstance(d, dict):
            raise ValueError("final.packet must be an object")
        return cls(
            specialist_id=d.get("specialist_id"),
            fax=d.get("fax"),
            documents=tuple(d.get("documents") or []),
            prior_auth_status=d.get("prior_auth_status"),
            fields=_as_dict(d.get("fields")),
            extra={k: v for k, v in d.items() if k not in PACKET_KEYS},
        )

    def to_dict(self) -> dict:
        return {
            "specialist_id": self.specialist_id,
            "fax": self.fax,
            "documents": list(self.documents),
            "prior_auth_status": self.prior_auth_status,
            "fields": self.fields,
            **self.extra,
        }


@dataclass(frozen=True)
class Final:
    action: str
    packet: Packet | None

    @classmethod
    def from_dict(cls, d: dict) -> "Final":
        action = d["action"]
        if action not in FINAL_ACTIONS:
            raise ValueError(f"final.action must be one of {FINAL_ACTIONS}, got {action!r}")
        packet = d.get("packet")
        if action == "sent" and not packet:
            raise ValueError("final.action is 'sent' but final.packet is empty: a sent run must say what was sent")
        return cls(action=action, packet=Packet.from_dict(packet) if packet else None)

    def to_dict(self) -> dict:
        return {"action": self.action, "packet": self.packet.to_dict() if self.packet else None}


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    @classmethod
    def from_dict(cls, d: dict) -> "Usage":
        d = d or {}
        if not isinstance(d, dict):
            raise ValueError("usage must be an object with input_tokens and output_tokens")
        return cls(input_tokens=int(d.get("input_tokens") or 0), output_tokens=int(d.get("output_tokens") or 0))

    def to_dict(self) -> dict:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


@dataclass(frozen=True)
class Run:
    run_id: str
    case_id: str
    agent_version: str
    model_label: str
    steps: tuple[Step, ...]
    final: Final
    usage: Usage

    @classmethod
    def from_dict(cls, d: dict) -> "Run":
        if not isinstance(d, dict):
            raise ValueError("each run must be a JSON object")
        name = d.get("run_id") or d.get("case_id") or "?"
        try:
            steps = []
            for i, s in enumerate(d.get("steps") or []):
                try:
                    steps.append(Step.from_dict(s))
                except ValueError as err:
                    raise ValueError(f"step {i + 1}: {err}") from None
            final = Final.from_dict(d["final"])
            if final.packet is None and any(s.tool == "send_to_specialist" for s in steps):
                raise ValueError("it calls send_to_specialist but final.packet is empty: say what was sent")
            return cls(
                run_id=d["run_id"],
                case_id=d["case_id"],
                agent_version=d["agent_version"],
                model_label=d.get("model_label") or "unknown-model",
                steps=tuple(steps),
                final=final,
                usage=Usage.from_dict(d.get("usage")),
            )
        except KeyError as err:
            raise ValueError(f"run {name}: missing field {err.args[0]!r}") from None
        except (ValueError, TypeError, AttributeError) as err:
            raise ValueError(f"run {name}: {err}") from None

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "case_id": self.case_id,
            "agent_version": self.agent_version,
            "model_label": self.model_label,
            "steps": [s.to_dict() for s in self.steps],
            "final": self.final.to_dict(),
            "usage": self.usage.to_dict(),
        }

    def indices(self, tool: str) -> list[int]:
        return [i for i, s in enumerate(self.steps) if s.tool == tool]


# Results: what shipcheck concluded


@dataclass(frozen=True)
class CheckResult:
    """One check on one run.

    expected/actual are exact, for engineers. plain_expected/plain_actual say
    the same thing in words a clinic manager would use.
    """

    check_id: str
    severity: str
    status: str
    step: int | None = None
    expected: str = ""
    actual: str = ""
    plain_expected: str = ""
    plain_actual: str = ""

    @property
    def failed(self) -> bool:
        return self.status == FAIL

    @property
    def applicable(self) -> bool:
        return self.status != NA

    def to_dict(self) -> dict:
        return {
            "check_id": self.check_id,
            "severity": self.severity,
            "status": self.status,
            "step": self.step,
            "expected": self.expected,
            "actual": self.actual,
            "plain_expected": self.plain_expected,
            "plain_actual": self.plain_actual,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CheckResult":
        return cls(d["check_id"], d["severity"], d["status"], d.get("step"), d.get("expected", ""), d.get("actual", ""),
                   d.get("plain_expected", ""), d.get("plain_actual", ""))


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    agent_version: str
    checks: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        """A case passes only if every applicable check passes."""
        return not any(c.failed for c in self.checks)

    def check(self, check_id: str) -> CheckResult:
        for c in self.checks:
            if c.check_id == check_id:
                return c
        raise KeyError(check_id)

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if c.failed]

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "agent_version": self.agent_version,
            "passed": self.passed,
            "checks": [c.to_dict() for c in self.checks],
        }


@dataclass(frozen=True)
class VersionSummary:
    agent_version: str
    model_label: str
    n_cases: int
    n_passed: int
    pass_rate: float
    interval: tuple[float, float]
    safety_issues: int
    quality_issues: int
    per_check: dict[str, dict[str, int]]
    tokens: dict[str, float]
    cost: dict[str, Any] | None = None

    def to_dict(self) -> dict:
        return {
            "agent_version": self.agent_version,
            "model_label": self.model_label,
            "n_cases": self.n_cases,
            "n_passed": self.n_passed,
            "pass_rate": self.pass_rate,
            "interval": list(self.interval),
            "safety_issues": self.safety_issues,
            "quality_issues": self.quality_issues,
            "per_check": self.per_check,
            "tokens": self.tokens,
            "cost": self.cost,
        }


@dataclass(frozen=True)
class CaseChange:
    """How one case moved between baseline and candidate, check by check.

    A new failure is a check that fails on the candidate and did not fail on the
    baseline (it passed, or did not apply). A resolved failure is a check that
    failed on the baseline and passes on the candidate. A check that goes from
    failing to not applying is neither: it did not get fixed, it went away,
    so it is listed separately in no_longer_applies.
    """

    case_id: str
    baseline_passed: bool
    candidate_passed: bool
    new_failures: tuple[str, ...]
    resolved: tuple[str, ...]
    no_longer_applies: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "baseline_passed": self.baseline_passed,
            "candidate_passed": self.candidate_passed,
            "new_failures": list(self.new_failures),
            "resolved": list(self.resolved),
            "no_longer_applies": list(self.no_longer_applies),
        }


@dataclass(frozen=True)
class Comparison:
    baseline: VersionSummary
    candidate: VersionSummary
    baseline_results: dict[str, CaseResult]
    candidate_results: dict[str, CaseResult]
    buckets: dict[str, list[str]]
    changes: tuple[CaseChange, ...]
    per_check: tuple[dict, ...]
    discordant: dict[str, int]
    p_value: float
    cost_delta: dict[str, Any]

    @property
    def case_ids(self) -> list[str]:
        return list(self.baseline_results)

    def to_dict(self) -> dict:
        return {
            "baseline": self.baseline.agent_version,
            "candidate": self.candidate.agent_version,
            "buckets": self.buckets,
            "changes": [c.to_dict() for c in self.changes],
            "per_check": list(self.per_check),
            "discordant": self.discordant,
            "p_value": self.p_value,
            "cost_delta": self.cost_delta,
        }


@dataclass(frozen=True)
class Verdict:
    level: str
    headline: str
    reasons: tuple[str, ...]
    supporting: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "headline": self.headline,
            "reasons": list(self.reasons),
            "supporting": list(self.supporting),
        }


# Loading from disk


def read_json(path: str | Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_suite(path: str | Path) -> Suite:
    """Load a suite folder holding workflow.json and cases.json."""
    path = Path(path)
    workflow = Workflow.from_dict(read_json(path / "workflow.json"))
    raw = read_json(path / "cases.json")
    case_list = raw["cases"] if isinstance(raw, dict) else raw
    cases = tuple(Case.from_dict(c) for c in case_list)
    ids = [c.case_id for c in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("cases.json has duplicate case_id values")
    synthetic = bool(isinstance(raw, dict) and raw.get("synthetic")) or bool(read_json(path / "workflow.json").get("synthetic"))
    return Suite(name=workflow.name, workflow=workflow, cases=cases, synthetic=synthetic)


def load_runs(path: str | Path) -> list[Run]:
    """Load runs from a JSON file: either a list of runs or {"runs": [...]}."""
    raw = read_json(path)
    run_list = raw.get("runs") if isinstance(raw, dict) else raw
    if not isinstance(run_list, list):
        raise ValueError(f"{path}: expected a list of runs or an object with a 'runs' list")
    return [Run.from_dict(r) for r in run_list]


def validate_suite(suite: Suite) -> list[str]:
    """Return a list of problems with the suite. An empty list means it is consistent.

    This is the phase 1 gate: every expected value must agree with the
    workflow definition, so a failing check always means the agent was wrong,
    never that the test case was.
    """
    wf = suite.workflow
    problems = []
    plans = set(wf.prior_auth_rules)
    for c in suite.cases:
        tag = f"{c.case_id}:"
        if c.insurance_plan not in plans:
            problems.append(f"{tag} unknown plan {c.insurance_plan}")
        if c.specialty not in wf.required_documents:
            problems.append(f"{tag} unknown specialty {c.specialty}")
            continue
        sp = wf.specialist(c.expected.specialist_id)
        if sp is None:
            problems.append(f"{tag} expected specialist {c.expected.specialist_id} is not in the directory")
        else:
            if sp["specialty"] != c.specialty:
                problems.append(f"{tag} expected specialist {sp['specialist_id']} is {sp['specialty']}, case is {c.specialty}")
            if c.insurance_plan not in sp["in_network_plans"]:
                problems.append(f"{tag} expected specialist {sp['specialist_id']} is not in network for {c.insurance_plan}")
            if not sp.get("accepting_new_patients", True):
                problems.append(f"{tag} expected specialist {sp['specialist_id']} is not accepting new patients")
        if c.expected.prior_auth_required != wf.prior_auth_required(c.insurance_plan, c.specialty):
            problems.append(f"{tag} prior_auth_required disagrees with the plan rules")
        if set(c.expected.required_documents) != set(wf.required_documents[c.specialty]):
            problems.append(f"{tag} required_documents disagree with the workflow for {c.specialty}")
        known_flags = set(wf.escalation_flags) | set(wf.routine_flags)
        for flag in c.urgency_flags:
            if flag not in known_flags:
                problems.append(f"{tag} unknown urgency flag {flag}")
        should_escalate = any(f in wf.escalation_flags for f in c.urgency_flags)
        if c.expected.must_escalate != should_escalate:
            problems.append(f"{tag} must_escalate disagrees with the escalation flags")
        if c.origin == "staff_flag" and not c.origin_note:
            problems.append(f"{tag} staff_flag case needs an origin_note")
    return problems
