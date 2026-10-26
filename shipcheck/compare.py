"""Paired comparison of a baseline and a candidate on the same cases.

Every case is scored on both versions, then compared case by case and check
by check. Pairing matters: two versions can share an average while trading
one set of failures for a different, worse set.
"""

from __future__ import annotations

from typing import Iterable

from shipcheck.checks import CHECKS, RANK, Check, evaluate_case
from shipcheck.cost import RateCard, cost_delta, estimate_cost, usage_totals
from shipcheck.model import (
    FAIL,
    NA,
    PASS,
    QUALITY,
    SAFETY,
    CaseChange,
    CaseResult,
    Comparison,
    Run,
    Suite,
    VersionSummary,
)
from shipcheck.stats import mcnemar_exact, wilson_interval


def index_runs(suite: Suite, runs: Iterable[Run]) -> dict[str, Run]:
    """Map case_id to run, insisting on exactly one run per suite case."""
    by_case: dict[str, Run] = {}
    for r in runs:
        if r.case_id in by_case:
            raise ValueError(f"two runs for case {r.case_id}")
        by_case[r.case_id] = r
    wanted = [c.case_id for c in suite.cases]
    missing = [cid for cid in wanted if cid not in by_case]
    extra = sorted(set(by_case) - set(wanted))
    if missing or extra:
        parts = []
        if missing:
            parts.append("missing runs for " + ", ".join(missing))
        if extra:
            parts.append("runs for unknown cases " + ", ".join(extra))
        raise ValueError("; ".join(parts))
    versions = {r.agent_version for r in by_case.values()}
    if len(versions) != 1:
        raise ValueError(f"runs mix agent versions: {sorted(versions)}")
    return {cid: by_case[cid] for cid in wanted}


def evaluate_runs(suite: Suite, runs: Iterable[Run], checks: Iterable[Check] = CHECKS) -> dict[str, CaseResult]:
    checks = tuple(checks)
    by_case = index_runs(suite, runs)
    return {c.case_id: evaluate_case(c, by_case[c.case_id], suite.workflow, checks) for c in suite.cases}


def summarize(
    results: dict[str, CaseResult],
    runs: Iterable[Run],
    rate_card: RateCard | None = None,
    checks: Iterable[Check] = CHECKS,
) -> VersionSummary:
    runs = list(runs)
    n = len(results)
    k = sum(r.passed for r in results.values())
    per_check = {}
    for chk in checks:
        counts = {PASS: 0, FAIL: 0, NA: 0}
        for r in results.values():
            counts[r.check(chk.id).status] += 1
        per_check[chk.id] = {"pass": counts[PASS], "fail": counts[FAIL], "na": counts[NA]}
    safety = sum(1 for r in results.values() for c in r.checks if c.failed and c.severity == SAFETY)
    quality = sum(1 for r in results.values() for c in r.checks if c.failed and c.severity == QUALITY)
    tokens = usage_totals(runs)
    labels = {r.model_label for r in runs}
    model_label = labels.pop() if len(labels) == 1 else "mixed"
    version = runs[0].agent_version if runs else "unknown"
    return VersionSummary(
        agent_version=version,
        model_label=model_label,
        n_cases=n,
        n_passed=k,
        pass_rate=k / n if n else 0.0,
        interval=wilson_interval(k, n),
        safety_issues=safety,
        quality_issues=quality,
        per_check=per_check,
        tokens=tokens,
        cost=estimate_cost(tokens, model_label, rate_card),
    )


def case_change(base: CaseResult, cand: CaseResult) -> CaseChange:
    new, resolved, gone = [], [], []
    for b in base.checks:
        c = cand.check(b.check_id)
        if c.status == FAIL and b.status != FAIL:
            new.append(b.check_id)
        elif b.status == FAIL and c.status == PASS:
            resolved.append(b.check_id)
        elif b.status == FAIL and c.status == NA:
            gone.append(b.check_id)
    for ids in (new, resolved, gone):
        ids.sort(key=RANK.get)
    return CaseChange(base.case_id, base.passed, cand.passed, tuple(new), tuple(resolved), tuple(gone))


def compare(
    suite: Suite,
    baseline_runs: Iterable[Run],
    candidate_runs: Iterable[Run],
    rate_card: RateCard | None = None,
    checks: Iterable[Check] = CHECKS,
) -> Comparison:
    checks = tuple(checks)
    baseline_runs, candidate_runs = list(baseline_runs), list(candidate_runs)
    base = evaluate_runs(suite, baseline_runs, checks)
    cand = evaluate_runs(suite, candidate_runs, checks)

    buckets: dict[str, list[str]] = {"fixed": [], "broken": [], "still_passing": [], "still_failing": []}
    changes = []
    for cid in base:
        b, c = base[cid].passed, cand[cid].passed
        key = {(False, True): "fixed", (True, False): "broken", (True, True): "still_passing", (False, False): "still_failing"}[(b, c)]
        buckets[key].append(cid)
        ch = case_change(base[cid], cand[cid])
        if ch.new_failures or ch.resolved or ch.no_longer_applies or b != c:
            changes.append(ch)

    per_check = []
    for chk in checks:
        new = sum(1 for ch in changes if chk.id in ch.new_failures)
        fixed = sum(1 for ch in changes if chk.id in ch.resolved)
        row = {"check_id": chk.id, "severity": chk.severity, "new_failures": new, "resolved": fixed}
        for side, results in (("baseline", base), ("candidate", cand)):
            statuses = [r.check(chk.id).status for r in results.values()]
            applicable = sum(s != NA for s in statuses)
            passed = sum(s == PASS for s in statuses)
            row[side] = {"pass": passed, "applicable": applicable, "rate": passed / applicable if applicable else None}
        b_rate, c_rate = row["baseline"]["rate"], row["candidate"]["rate"]
        row["change"] = None if b_rate is None or c_rate is None else c_rate - b_rate
        per_check.append(row)

    base_summary = summarize(base, baseline_runs, rate_card, checks)
    cand_summary = summarize(cand, candidate_runs, rate_card, checks)
    b_count, c_count = len(buckets["fixed"]), len(buckets["broken"])
    return Comparison(
        baseline=base_summary,
        candidate=cand_summary,
        baseline_results=base,
        candidate_results=cand,
        buckets=buckets,
        changes=tuple(changes),
        per_check=tuple(per_check),
        discordant={"fixed": b_count, "broken": c_count},
        p_value=mcnemar_exact(b_count, c_count),
        cost_delta=cost_delta(base_summary.tokens, cand_summary.tokens, base_summary.cost, cand_summary.cost),
    )
