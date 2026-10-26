"""Results document and the one-file HTML report.

build_results() turns runs into the results document that is written as
results.json. render_html() turns that same document into a single HTML file
with inline CSS and JS and the data embedded as JSON, so it opens from disk
with no network and shows exactly the numbers in results.json.
"""

from __future__ import annotations

import html
import json
import shlex
from pathlib import Path
from typing import Iterable

from shipcheck import __version__
from shipcheck.checks import BY_ID, CHECKS, RANK
from shipcheck.compare import compare
from shipcheck.cost import RateCard
from shipcheck.gate import (
    change_text,
    correct_text,
    decide,
    interval_text,
    money_text,
    p_text,
    pct,
    plural,
    significance_text,
    tokens_text,
    wide_interval_text,
)
from shipcheck.model import BLOCK, FAIL, REVIEW, Run, Step, Suite

NOTICE = "All cases and agent runs are synthetic and simulated. No real patient data. Example rates are not real prices."
SYNTHETIC_CASES_NOTICE = "The test cases are synthetic. No real patient data."
DEFAULT_RESULTS = "examples/results.json"
DEFAULT_APPROVER = "Clinic Ops Lead"


# The results document


def _commands(base: str, cand: str, level: str, worse: list[str], results_path: str | None) -> dict:
    extra = ""
    if results_path and Path(results_path).as_posix() != DEFAULT_RESULTS:
        extra = f" --results {shlex.quote(Path(results_path).as_posix())}"
    note = "Reviewed " + ", ".join(worse) if level == REVIEW and worse else "No cases got worse"
    approve = None
    if level != BLOCK:
        approve = f'shipcheck approve --candidate {cand} --by "{DEFAULT_APPROVER}" --note "{note}"{extra}'
    rollback = f'shipcheck rollback --candidate {cand} --by "{DEFAULT_APPROVER}" --note "Keep {base}. {cand} was {level}."{extra}'
    return {"approve": approve, "rollback": rollback}


def _notice(simulated: bool, suite: Suite, rate_card: RateCard | None) -> str:
    if simulated:
        return NOTICE
    parts = [SYNTHETIC_CASES_NOTICE] if suite.synthetic else []
    if rate_card:
        parts.append(f"Dollar figures use {rate_card.label}.")
    return " ".join(parts)


def build_results(
    suite: Suite,
    runs_by_version: dict[str, list[Run]],
    pairs: Iterable[tuple[str, str]],
    rate_card: RateCard | None = None,
    descriptions: dict[str, str] | None = None,
    simulated: bool = True,
    results_path: str | None = DEFAULT_RESULTS,
) -> dict:
    descriptions = descriptions or {}
    versions: dict[str, dict] = {}
    results: dict[str, dict] = {}
    comparisons = []
    for base, cand in pairs:
        cmp = compare(suite, runs_by_version[base], runs_by_version[cand], rate_card)
        verdict = decide(cmp)
        for name, summary, res in ((base, cmp.baseline, cmp.baseline_results), (cand, cmp.candidate, cmp.candidate_results)):
            if name not in versions:
                versions[name] = {"description": descriptions.get(name, ""), **summary.to_dict()}
                results[name] = {cid: r.to_dict() for cid, r in res.items()}
        worse = [ch.case_id for ch in cmp.changes if ch.new_failures]
        comparisons.append({
            "id": f"{cand}-vs-{base}",
            "title": f"{cand} vs {base}",
            **cmp.to_dict(),
            "verdict": verdict.to_dict(),
            "commands": _commands(base, cand, verdict.level, worse, results_path),
        })
    return {
        "tool": "shipcheck",
        "version": __version__,
        "simulated": simulated,
        "synthetic_cases": suite.synthetic,
        "notice": _notice(simulated, suite, rate_card),
        "suite": {
            "name": suite.name,
            "title": suite.workflow.title,
            "cases": len(suite.cases),
            "document_labels": dict(suite.workflow.document_labels),
        },
        "rate_card": rate_card.to_dict() if rate_card else None,
        "checks": [c.describe() for c in CHECKS],
        "versions": versions,
        "comparisons": comparisons,
        "cases": {c.case_id: c.to_dict() for c in suite.cases},
        "results": results,
    }


# Plain-language step summaries for the referral workflow. Unknown tools fall
# back to their name, so the report still renders for other workflows.


def describe_step(step: Step, labels: dict[str, str] | None = None) -> str:
    labels = labels or {}
    a, r, t = step.args, step.result, step.tool

    def doc(name):
        return labels.get(name, str(name).replace("_", " "))

    if t == "get_referral_order":
        return f"Read referral order {r.get('order_id') or a.get('order_id', '')}".strip()
    if t == "get_insurance":
        return f"Looked up insurance: plan {r.get('plan_id', '?')}, coverage {r.get('coverage_status', '?')}"
    if t == "check_prior_auth_rules":
        return "Checked prior authorization rules: " + ("required" if r.get("prior_auth_required") else "not required")
    if t == "search_specialist_directory":
        return f"Searched the specialist directory ({plural(len(r.get('results', [])), 'result')})"
    if t == "attach_document":
        return f"Attached the {doc(a.get('document') or r.get('attached', 'document'))}"
    if t == "draft_referral_packet":
        p = a.get("packet") or {}
        return f"Drafted the packet for {p.get('specialist_id', '?')} with fax {p.get('fax', '?')}"
    if t == "request_staff_approval":
        answer = r.get("approved")
        said = "approved" if answer is True else ("declined" if answer is False else "no answer")
        note = f" ({r['note']})" if r.get("note") else ""
        return f"Asked staff to approve: {said}{note}"
    if t == "send_to_specialist":
        return f"Sent to {a.get('specialist_id', '?')} by fax {a.get('fax', '?')}"
    if t == "escalate_to_staff":
        return f"Handed the case to staff: {a.get('reason', '')}".strip()
    return t


# HTML


def e(x) -> str:
    return html.escape(str(x), quote=True)


def _rate(k: int, n: int) -> str:
    return f"{k} of {n} ({pct(k / n)})" if n else "n/a"


def _points(before: dict, after: dict) -> str:
    """Change in percentage points, computed from the displayed percentages so a row never disagrees with itself."""
    if not before["applicable"] or not after["applicable"]:
        return "n/a"
    pts = int(pct(after["rate"])[:-1]) - int(pct(before["rate"])[:-1])
    if pts == 0:
        return "no change"
    return f"{'+' if pts > 0 else '-'}{abs(pts)} pts"


def _steps_list(run: dict, flagged: dict[int, str]) -> str:
    items = []
    for i, step in enumerate(run["steps"]):
        cls = ' class="flag"' if i in flagged else ""
        mark = f' <span class="flag-note">{e(flagged[i])}</span>' if i in flagged else ""
        items.append(f'<li{cls}><span class="step-n">{i + 1}</span><span>{e(step["plain"])}{mark}</span></li>')
    end = flagged.get(-1)
    end_mark = f' <span class="flag-note">{e(end)}</span>' if end else ""
    items.append(f'<li class="final{" flag" if end else ""}"><span class="step-n">end</span>'
                 f'<span>Final action: {e(run["final"]["action"])}{end_mark}</span></li>')
    return '<ol class="steps">' + "".join(items) + "</ol>"


def _flags(result: dict, check_ids: Iterable[str]) -> dict[int, str]:
    out: dict[int, str] = {}
    for c in result["checks"]:
        if c["check_id"] in check_ids and c["status"] == FAIL:
            out.setdefault(c["step"] if c["step"] is not None else -1, "Problem here")
    return out


def _check_box(check_id: str, base: str, b_res: dict, cand: str, c_res: dict) -> str:
    chk = BY_ID[check_id]
    b = next(x for x in b_res["checks"] if x["check_id"] == check_id)
    c = next(x for x in c_res["checks"] if x["check_id"] == check_id)
    expected = c["plain_expected"] or b["plain_expected"]
    return (
        f'<div class="evidence"><p class="ev-title"><span class="sev sev-{chk.severity}">{e(chk.severity)}</span> '
        f'{e(chk.plain_name)}</p><dl>'
        f'<dt>Expected</dt><dd>{e(expected)}</dd>'
        f'<dt>Before ({e(base)})</dt><dd>{e(b["plain_actual"])}</dd>'
        f'<dt>Now ({e(cand)})</dt><dd>{e(c["plain_actual"])}</dd>'
        "</dl></div>"
    )


def _change_item(doc: dict, cmp: dict, change: dict, kind: str, runs: dict) -> str:
    base, cand = cmp["baseline"], cmp["candidate"]
    cid = change["case_id"]
    case = doc["cases"][cid]
    b_res, c_res = doc["results"][base][cid], doc["results"][cand][cid]
    if kind == "worse":
        ids = list(change["new_failures"])
        lead = BY_ID[ids[0]]
        sentence, sev = lead.plain_failure_text, lead.severity
    elif change["resolved"]:
        ids = list(change["resolved"])
        lead = BY_ID[ids[0]]
        sentence, sev = lead.plain_fixed_text, lead.severity
    else:
        ids = list(change["no_longer_applies"])
        sentence, sev = "Now passes, because the check it failed no longer applies to it.", "quality"
    more = f' <span class="more">+{len(ids) - 1} more</span>' if len(ids) > 1 else ""
    context = f"{case['specialty'].capitalize()} referral, plan {case['insurance_plan']}: {case['referral_reason']}."
    origin = f'<p class="origin">{e(case["origin_note"])} This case was added to the suite from that flag.</p>' if case.get("origin_note") else ""
    boxes = "".join(_check_box(chk, base, b_res, cand, c_res) for chk in ids)
    also = ""
    if kind == "worse" and change["resolved"]:
        also = '<p class="also">Also fixed on this case: ' + e(" ".join(BY_ID[x].plain_fixed_text for x in change["resolved"])) + "</p>"
    steps = (
        '<div class="two">'
        f'<div><p class="col-h">Steps in {e(cand)} (proposed)</p>{_steps_list(runs[cand][cid], _flags(c_res, change["new_failures"]))}</div>'
        f'<div><p class="col-h">Steps in {e(base)} (current)</p>{_steps_list(runs[base][cid], _flags(b_res, change["resolved"] + change["no_longer_applies"]))}</div>'
        "</div>"
    )
    return (
        f'<li class="change {kind}"><details><summary><span class="dot sev-{sev}" aria-hidden="true"></span>'
        f'<span class="sum-text"><strong>Case {e(cid)}:</strong> {e(sentence)}{more}</span>'
        '<span class="marker" aria-hidden="true"></span></summary>'
        f'<div class="change-body"><p class="context">{e(context)}</p>{origin}{boxes}{also}{steps}'
        f'<p class="trace-link"><button type="button" class="linkish" data-trace="{e(cid)}">Open the full trace for {e(cid)}</button></p>'
        "</div></details></li>"
    )


def _sorted_changes(cmp: dict, kind: str) -> list[dict]:
    """Got worse: any new failure. Got better: no new failure, and a fix or a flip to passing. Worst first."""
    if kind == "worse":
        items = [c for c in cmp["changes"] if c["new_failures"]]
        return sorted(items, key=lambda c: (min(RANK[x] for x in c["new_failures"]), c["case_id"]))
    items = [c for c in cmp["changes"] if not c["new_failures"]
             and (c["resolved"] or (c["candidate_passed"] and not c["baseline_passed"]))]
    return sorted(items, key=lambda c: (min(RANK[x] for x in (c["resolved"] or c["no_longer_applies"])), c["case_id"]))


def _no_cost_reason(doc: dict, v: dict) -> str:
    if not doc["rate_card"]:
        return "No rate card given, so no dollar estimate."
    if v["model_label"] == "mixed":
        return "These runs use more than one model, so no single rate applies."
    return f"The rate card has no rate for {v['model_label']}, so no dollar estimate."


def _metric_card(doc: dict, version: str, role: str, other: str | None) -> str:
    v = doc["versions"][version]
    n, k = v["n_cases"], v["n_passed"]
    cost = v["cost"]
    cost_line = (
        f'<p class="metric-sub">{e(money_text(cost["per_1000_runs"], cost["currency"]))} '
        f'<span class="rate-tag">{e(cost["label"])}</span></p>'
        if cost else f'<p class="metric-sub">{e(_no_cost_reason(doc, v))}</p>'
    )
    safety = v["safety_issues"]
    worse = other is not None and safety > doc["versions"][other]["safety_issues"]
    desc = f'<p class="card-desc">{e(v["description"])}</p>' if v.get("description") else ""
    return f"""
<article class="card">
  <p class="card-role">{e(role)}</p>
  <h3>{e(version)} <span class="model">{e(v["model_label"])}</span></h3>
  {desc}
  <div class="metric">
    <p class="metric-name">Correct</p>
    <p class="metric-value">{e(correct_text(k, n))}</p>
    <p class="metric-sub">{e(interval_text(v["interval"]))}</p>
  </div>
  <div class="metric">
    <p class="metric-name">Cost per 1,000 runs</p>
    <p class="metric-value">{e(tokens_text(v["tokens"]["total_per_1000_runs"]))} tokens</p>
    {cost_line}
  </div>
  <div class="metric">
    <p class="metric-name">Safety issues</p>
    <p class="metric-value{" bad" if worse else ""}">{safety}</p>
    <p class="metric-sub">{"safety check failure" if safety == 1 else "safety check failures"} across {n} cases</p>
  </div>
</article>"""


def _wide_note(doc: dict, cmp: dict) -> str:
    vb, vc = doc["versions"][cmp["baseline"]], doc["versions"][cmp["candidate"]]
    return wide_interval_text(vb["n_cases"], [vb["interval"], vc["interval"]]) or ""


def _delta_strip(doc: dict, cmp: dict) -> str:
    base, cand = doc["versions"][cmp["baseline"]], doc["versions"][cmp["candidate"]]
    d = cmp["cost_delta"]
    diff_cases = cand["n_passed"] - base["n_passed"]
    diff_safety = cand["safety_issues"] - base["safety_issues"]
    items = [
        ("Correct", "same" if diff_cases == 0 else f"{'+' if diff_cases > 0 else '-'}{plural(abs(diff_cases), 'case')}"),
        ("Tokens per run", change_text(d["tokens_change"])),
    ]
    if d["cost_change"] is not None:
        items.append(("Cost at example rates" if doc["simulated"] else "Estimated cost", change_text(d["cost_change"])))
    items.append(("Safety issues", "same" if diff_safety == 0 else f"{'+' if diff_safety > 0 else '-'}{abs(diff_safety)}"))
    cells = "".join(f'<div><p class="d-name">{e(a)}</p><p class="d-val">{e(b)}</p></div>' for a, b in items)
    return f'<div class="delta" role="group" aria-label="Change from {e(cmp["baseline"])} to {e(cmp["candidate"])}">{cells}</div>'


def _verdict_block(cmp: dict) -> str:
    v = cmp["verdict"]
    level = v["level"]
    reasons = v["reasons"]
    top = "".join(f"<li>{e(r)}</li>" for r in reasons[:3])
    rest = ""
    if len(reasons) > 3:
        rest = (
            f'<details class="more-reasons"><summary>{plural(len(reasons) - 3, "more reason")}</summary>'
            f'<ol start="4">{"".join(f"<li>{e(r)}</li>" for r in reasons[3:])}</ol></details>'
        )
    word = {"BLOCK": "Blocked", "REVIEW": "Needs review", "SHIP": "No regressions found"}[level]
    return f"""
<section class="verdict verdict-{level.lower()}" aria-label="Verdict">
  <div class="verdict-top">
    <span class="badge">{e(level)}</span>
    <span class="verdict-word">{e(word)}: {e(cmp["candidate"])} compared with {e(cmp["baseline"])}</span>
  </div>
  <p class="headline">{e(v["headline"])}</p>
  <ol class="reasons">{top}</ol>
  {rest}
</section>"""


def _decision(cmp: dict) -> str:
    cmds = cmp["commands"]
    cid, cand, base = cmp["id"], cmp["candidate"], cmp["baseline"]
    level = cmp["verdict"]["level"]
    if cmds["approve"]:
        approve = f'<button type="button" class="btn btn-primary" data-cmd="{e(cmds["approve"])}">Approve {e(cand)}</button>'
    else:
        approve = f'<button type="button" class="btn btn-primary" disabled aria-describedby="why-{e(cid)}">Approve {e(cand)}</button>'
    blocked = (
        f'<p class="why" id="why-{e(cid)}">A blocked change cannot be approved. Fix the safety failures, run shipcheck again, '
        "and approve the new version.</p>" if level == BLOCK else ""
    )
    intro = {
        "BLOCK": f"shipcheck blocked {cand}. You can record that you are keeping {base}.",
        "REVIEW": f"Look at the cases that got worse above. If you accept them, approve {cand}. If not, keep {base}.",
        "SHIP": f"Nothing got worse. You can approve {cand}.",
    }[level]
    return f"""
<section class="decide" aria-label="Approve or roll back">
  <h2>Approve or roll back</h2>
  <p>{e(intro)}</p>
  <div class="btns">
    {approve}
    <button type="button" class="btn" data-cmd="{e(cmds["rollback"])}">Roll back to {e(base)}</button>
  </div>
  {blocked}
  <div class="cmd" hidden>
    <p>This page is a static file and cannot record anything. To record the decision in the approvals log, run this from the repo folder:</p>
    <pre tabindex="-1"><code></code></pre>
    <button type="button" class="btn btn-small copy">Copy command</button>
  </div>
  <p class="hint cmd-status" aria-live="polite"></p>
</section>"""


def _manager_view(doc: dict, cmp: dict, runs: dict) -> str:
    worse = _sorted_changes(cmp, "worse")
    better = _sorted_changes(cmp, "better")
    worse_html = "".join(_change_item(doc, cmp, c, "worse", runs) for c in worse) or '<li class="none">No case got worse.</li>'
    better_html = "".join(_change_item(doc, cmp, c, "better", runs) for c in better) or '<li class="none">No case got better.</li>'
    rc = doc["rate_card"]
    rate_note = (
        f'<p class="rate-note">Dollar figures use <strong>{e(rc["label"])}</strong>. Source: {e(rc["source"])} Date: {e(rc["as_of"])}.</p>'
        if rc else ""
    )
    wide = _wide_note(doc, cmp)
    wide_html = f'<p class="wide-note">{e(wide)} The verdict does not rely on them.</p>' if wide else ""
    return f"""
<div class="cards">
  {_metric_card(doc, cmp["baseline"], "Current", None)}
  {_metric_card(doc, cmp["candidate"], "Proposed", cmp["baseline"])}
</div>
{_delta_strip(doc, cmp)}
{wide_html}
{rate_note}
<section class="changed" aria-label="What changed">
  <h2>What changed</h2>
  <p class="hint">One line per case, worst first. Open a case to see what the agent did, step by step.</p>
  <div class="lists">
    <div><h3 class="worse-h">Got worse ({len(worse)})</h3><ul class="change-list">{worse_html}</ul></div>
    <div><h3 class="better-h">Got better ({len(better)})</h3><ul class="change-list">{better_html}</ul></div>
  </div>
</section>
{_decision(cmp)}"""


def _engineer_view(doc: dict, cmp: dict) -> str:
    base, cand = cmp["baseline"], cmp["candidate"]
    rows = []
    for row in cmp["per_check"]:
        chk = BY_ID[row["check_id"]]
        b, c = row["baseline"], row["candidate"]
        bad = " bad" if row["new_failures"] else ""
        rows.append(
            f'<tr><td><code>{e(chk.id)}</code><br><span class="muted">{e(chk.plain_name)}</span></td>'
            f'<td><span class="sev sev-{chk.severity}">{e(chk.severity)}</span></td>'
            f'<td>{e(_rate(b["pass"], b["applicable"]))}</td><td>{e(_rate(c["pass"], c["applicable"]))}</td>'
            f'<td>{e(_points(b, c))}</td><td class="num{bad}">{row["new_failures"]}</td><td class="num">{row["resolved"]}</td></tr>'
        )
    vb, vc = doc["versions"][base], doc["versions"][cand]
    fixed, broken = cmp["discordant"]["fixed"], cmp["discordant"]["broken"]
    tok_rows = []
    for name, v in ((base, vb), (cand, vc)):
        t = v["tokens"]
        cost = e(money_text(v["cost"]["per_1000_runs"], v["cost"]["currency"])) if v["cost"] else "n/a"
        tok_rows.append(
            f'<tr><td>{e(name)}</td><td>{e(v["model_label"])}</td><td class="num">{t["input_per_run"]:,.0f}</td>'
            f'<td class="num">{t["output_per_run"]:,.0f}</td><td class="num">{t["total_per_run"]:,.0f}</td>'
            f'<td class="num">{t["total_per_1000_runs"]:,}</td><td class="num">{cost}</td></tr>'
        )
    rc = doc["rate_card"]
    if rc:
        rates = "; ".join(
            f'{e(m)} input {e(money_text(r["input"], rc["currency"]))}, output {e(money_text(r["output"], rc["currency"]))}'
            for m, r in rc["models"].items())
        rate_caption = (f'<p class="rate-note">Cost column: <strong>{e(rc["label"])}</strong>. Source: {e(rc["source"])} '
                        f'Date: {e(rc["as_of"])}. Per {rc["per_tokens"]:,} tokens: {rates}.</p>')
        missing = [v for v in (vb, vc) if not v["cost"]]
        if missing:
            rate_caption += "".join(f'<p class="rate-note">{e(_no_cost_reason(doc, v))}</p>' for v in missing)
    else:
        rate_caption = '<p class="rate-note">No rate card given. Pass --rates to estimate cost.</p>'
    options = []
    changed = {c["case_id"] for c in cmp["changes"]}
    for cid, case in doc["cases"].items():
        mark = " (changed)" if cid in changed else ""
        options.append(f'<option value="{e(cid)}">{e(cid)}: {e(case["specialty"])}, {e(case["insurance_plan"])}{e(mark)}</option>')
    ordered = [c["case_id"] for c in _sorted_changes(cmp, "worse") + _sorted_changes(cmp, "better")]
    first = ordered[0] if ordered else next(iter(doc["cases"]))
    wide = _wide_note(doc, cmp)
    wide_html = f"<p>{e(wide)}</p>" if wide else ""
    return f"""
<section aria-label="Per-check results">
  <h2>Per-check results</h2>
  <p class="hint">Pass rates count only the cases where a check applies. A new failure is a case where the check fails on {e(cand)} and did not fail on {e(base)}.</p>
  <div class="table-wrap" tabindex="0" role="region" aria-label="Per-check results table"><table>
    <thead><tr><th scope="col">Check</th><th scope="col">Severity</th><th scope="col">{e(base)} passed</th><th scope="col">{e(cand)} passed</th><th scope="col">Change</th><th scope="col">New failures</th><th scope="col">Fixed</th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table></div>
</section>
<section aria-label="Statistics">
  <h2>Statistics</h2>
  <div class="stats">
    <div><p class="d-name">Pass rate, {e(base)}</p><p class="d-val">{e(correct_text(vb["n_passed"], vb["n_cases"]))}</p><p class="muted">Wilson 95%: {e(pct(vb["interval"][0]))} to {e(pct(vb["interval"][1]))}</p></div>
    <div><p class="d-name">Pass rate, {e(cand)}</p><p class="d-val">{e(correct_text(vc["n_passed"], vc["n_cases"]))}</p><p class="muted">Wilson 95%: {e(pct(vc["interval"][0]))} to {e(pct(vc["interval"][1]))}</p></div>
    <div><p class="d-name">Discordant cases</p><p class="d-val">{fixed} fixed, {broken} broken</p><p class="muted">cases that flipped between pass and fail</p></div>
    <div><p class="d-name">McNemar exact, two-sided</p><p class="d-val">{e(p_text(cmp["p_value"]))}</p><p class="muted">on the {fixed + broken} discordant cases</p></div>
  </div>
  <p>{e(significance_text(cmp["p_value"], fixed + broken))}</p>
  {wide_html}
  <p class="muted">The verdict does not depend on these statistics. It depends on which cases got worse, check by check. The numbers are here so nobody over-reads a small change in the average.</p>
</section>
<section aria-label="Tokens and cost">
  <h2>Tokens and cost</h2>
  <div class="table-wrap" tabindex="0" role="region" aria-label="Tokens and cost table"><table>
    <thead><tr><th scope="col">Version</th><th scope="col">Model</th><th scope="col">Input per run</th><th scope="col">Output per run</th><th scope="col">Total per run</th><th scope="col">Tokens per 1,000 runs</th><th scope="col">Est. cost per 1,000 runs</th></tr></thead>
    <tbody>{"".join(tok_rows)}</tbody>
  </table></div>
  {rate_caption}
</section>
<section class="trace" aria-label="Trace viewer">
  <h2>Trace viewer</h2>
  <p class="hint">Every step the agent took, with the exact arguments and results. Failing steps are highlighted.</p>
  <div class="trace-controls">
    <label>Case <select class="trace-case">{"".join(options)}</select></label>
    <div class="seg" role="group" aria-label="Version">
      <button type="button" class="seg-btn" aria-pressed="true" data-version="{e(cand)}">{e(cand)} (proposed)</button>
      <button type="button" class="seg-btn" aria-pressed="false" data-version="{e(base)}">{e(base)} (current)</button>
    </div>
  </div>
  <p class="sr-only trace-status" aria-live="polite"></p>
  <div class="trace-out" data-first="{e(first)}"></div>
</section>"""


def _comparison_section(doc: dict, cmp: dict, runs: dict, active: bool) -> str:
    cid = cmp["id"]
    return f"""
<section class="cmp" id="cmp-{e(cid)}" data-cmp="{e(cid)}" data-baseline="{e(cmp["baseline"])}" data-candidate="{e(cmp["candidate"])}" aria-label="{e(cmp["title"])}"{"" if active else " hidden"}>
  {_verdict_block(cmp)}
  <div class="tabs" role="tablist" aria-label="View">
    <button type="button" role="tab" class="tab" aria-selected="true" tabindex="0" aria-controls="panel-m-{e(cid)}" data-view="manager" id="tab-m-{e(cid)}">Summary</button>
    <button type="button" role="tab" class="tab" aria-selected="false" tabindex="-1" aria-controls="panel-e-{e(cid)}" data-view="engineer" id="tab-e-{e(cid)}">Engineer view</button>
  </div>
  <div class="view" data-view="manager" role="tabpanel" id="panel-m-{e(cid)}" aria-labelledby="tab-m-{e(cid)}">{_manager_view(doc, cmp, runs)}</div>
  <div class="view" data-view="engineer" role="tabpanel" id="panel-e-{e(cid)}" aria-labelledby="tab-e-{e(cid)}" hidden>{_engineer_view(doc, cmp)}</div>
</section>"""


def _embedded_runs(runs_by_version: dict[str, list[Run]], labels: dict[str, str]) -> dict:
    out = {}
    for version, runs in runs_by_version.items():
        out[version] = {}
        for r in runs:
            d = r.to_dict()
            for step, s in zip(d["steps"], r.steps, strict=True):
                step["plain"] = describe_step(s, labels)
            out[version][r.case_id] = d
    return out


def render_html(doc: dict, runs_by_version: dict[str, list[Run]]) -> str:
    runs = _embedded_runs({v: runs_by_version[v] for v in doc["versions"]}, doc["suite"].get("document_labels", {}))
    comparisons = doc["comparisons"]
    switch = ""
    if len(comparisons) > 1:
        buttons = "".join(
            f'<button type="button" class="switch-btn" aria-pressed="{"true" if i == 0 else "false"}" '
            f'aria-controls="cmp-{e(c["id"])}" data-cmp="{e(c["id"])}"><span class="switch-title">{e(c["title"])}</span>'
            f'<span class="pill pill-{c["verdict"]["level"].lower()}">{e(c["verdict"]["level"])}</span></button>'
            for i, c in enumerate(comparisons)
        )
        switch = f'<div class="switch" role="group" aria-label="Which change to look at">{buttons}</div>'
    sections = "".join(_comparison_section(doc, c, runs, i == 0) for i, c in enumerate(comparisons))
    data = json.dumps({"doc": doc, "runs": runs}, sort_keys=True, separators=(",", ":"))
    data = data.replace("</", "<\\/").replace("<!--", "<\\!--")
    if doc["simulated"]:
        sim = ('<p class="sim-note" role="note"><strong>Simulated data.</strong> All cases and agent runs are synthetic. '
               "No real patients, providers, plans or phone numbers.</p>")
    elif doc.get("synthetic_cases"):
        sim = f'<p class="sim-note" role="note"><strong>Synthetic test cases.</strong> {e(SYNTHETIC_CASES_NOTICE)}</p>'
    else:
        sim = ""
    footer = f"<p>{e(doc['notice'])}</p>" if doc["notice"] else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>shipcheck report</title>
<style>{CSS}</style>
<noscript><style>section.cmp[hidden], .view[hidden] {{ display: block !important; }} .switch, .tabs, .btns, .trace {{ display: none; }}</style></noscript>
</head>
<body>
<header class="top">
  <div class="wrap">
    <p class="brand">shipcheck</p>
    <h1>Is this change safe to ship?</h1>
    <p class="sub">{e(doc["suite"]["title"])}. {doc["suite"]["cases"]} test cases, each run on both versions.</p>
    {sim}
    <noscript><p class="sim-note">JavaScript is off, so every comparison and view is shown below, one after another.</p></noscript>
  </div>
</header>
<main class="wrap">
{switch}
{sections}
</main>
<footer class="wrap foot">
  {footer}
  <p class="muted">Generated by shipcheck {e(doc["version"])}. The checks are code, not a model's opinion.</p>
</footer>
<script type="application/json" id="shipcheck-data">{data}</script>
<script>{JS}</script>
</body>
</html>
"""


def write_report(path: str | Path, doc: dict, runs_by_version: dict[str, list[Run]]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(doc, runs_by_version), encoding="utf-8")
    return path


CSS = """
:root {
  --bg: #f6f7f9; --surface: #ffffff; --surface-2: #f0f2f5; --text: #1c2128; --muted: #586270;
  --border: #d9dee4; --accent: #0d6a74; --accent-ink: #ffffff; --focus: #0d6a74;
  --block-bg: #fdeeee; --block-edge: #b42318; --block-ink: #7a1a12; --block-badge: #b42318; --block-badge-ink: #ffffff;
  --review-bg: #fff5e1; --review-edge: #a35c00; --review-ink: #5f3500; --review-badge: #8f5100; --review-badge-ink: #ffffff;
  --ship-bg: #e8f5ee; --ship-edge: #19733f; --ship-ink: #0d4426; --ship-badge: #19733f; --ship-badge-ink: #ffffff;
  --safety: #b42318; --quality: #8f5100; --flag-bg: #fdeeee;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1418; --surface: #171d23; --surface-2: #1e252d; --text: #e5e9ed; --muted: #a0aab4;
    --border: #2c353f; --accent: #5cbcc6; --accent-ink: #061417; --focus: #5cbcc6;
    --block-bg: #361616; --block-edge: #f08679; --block-ink: #ffdcd6; --block-badge: #f08679; --block-badge-ink: #1f0703;
    --review-bg: #33270f; --review-edge: #f0b75e; --review-ink: #ffe8c4; --review-badge: #f0b75e; --review-badge-ink: #1f1300;
    --ship-bg: #10301f; --ship-edge: #62cf91; --ship-ink: #d4f5e2; --ship-badge: #62cf91; --ship-badge-ink: #04140a;
    --safety: #f39a8f; --quality: #f0b75e; --flag-bg: #3a1a1a;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
.wrap { max-width: 1040px; margin: 0 auto; padding: 0 16px; }
h1, h2, h3 { line-height: 1.25; margin: 0; }
h1 { font-size: 1.75rem; font-weight: 650; letter-spacing: -0.01em; }
h2 { font-size: 1.2rem; font-weight: 650; margin: 0 0 .5rem; }
h3 { font-size: 1.05rem; font-weight: 650; }
p { margin: 0 0 .5rem; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .85rem; }
.muted { color: var(--muted); }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
button { font: inherit; color: inherit; }
:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; border-radius: 4px; }

.top { background: var(--surface); border-bottom: 1px solid var(--border); padding: 28px 0 20px; }
.brand { color: var(--accent); font-weight: 700; letter-spacing: .02em; margin-bottom: .25rem; }
.sub { color: var(--muted); margin: .35rem 0 .75rem; }
.sim-note { display: inline-block; background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 8px; padding: 6px 12px; font-size: .9rem; margin: 0; }

main.wrap { padding-top: 24px; padding-bottom: 24px; }
section { margin: 0 0 28px; }

.switch { display: flex; gap: 8px; margin-bottom: 20px; flex-wrap: wrap; }
.switch-btn { display: inline-flex; align-items: center; gap: 10px; background: var(--surface);
  border: 1px solid var(--border); border-radius: 10px; padding: 10px 14px; cursor: pointer; min-height: 44px; }
.switch-btn[aria-pressed="true"] { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
.switch-title { font-weight: 600; }
.pill { font-size: .75rem; font-weight: 700; letter-spacing: .04em; border-radius: 999px; padding: 2px 8px; }
.pill-block { background: var(--block-badge); color: var(--block-badge-ink); }
.pill-review { background: var(--review-badge); color: var(--review-badge-ink); }
.pill-ship { background: var(--ship-badge); color: var(--ship-badge-ink); }

.verdict { border-radius: 12px; padding: 20px 22px; border: 1px solid; border-left-width: 6px; }
.verdict-block { background: var(--block-bg); border-color: var(--block-edge); color: var(--block-ink); }
.verdict-review { background: var(--review-bg); border-color: var(--review-edge); color: var(--review-ink); }
.verdict-ship { background: var(--ship-bg); border-color: var(--ship-edge); color: var(--ship-ink); }
.verdict-top { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 10px; }
.badge { font-weight: 800; letter-spacing: .08em; font-size: 1.05rem; border-radius: 6px; padding: 4px 12px; }
.verdict-block .badge { background: var(--block-badge); color: var(--block-badge-ink); }
.verdict-review .badge { background: var(--review-badge); color: var(--review-badge-ink); }
.verdict-ship .badge { background: var(--ship-badge); color: var(--ship-badge-ink); }
.verdict-word { font-weight: 600; }
.headline { font-size: 1.2rem; font-weight: 600; margin-bottom: 10px; }
.reasons { margin: 0; padding-left: 1.4rem; }
.reasons li { margin: 4px 0; }
.more-reasons { margin-top: 6px; }
.more-reasons summary { cursor: pointer; font-weight: 600; }
.more-reasons ol { margin: 6px 0 0; padding-left: 1.4rem; }

.tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--border); margin: 24px 0 20px; }
.tab { background: none; border: 0; border-bottom: 3px solid transparent; padding: 10px 14px; cursor: pointer;
  color: var(--muted); font-weight: 600; min-height: 44px; }
.tab[aria-selected="true"] { color: var(--text); border-bottom-color: var(--accent); }

.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px; }
.card-role { text-transform: uppercase; font-size: .75rem; letter-spacing: .08em; color: var(--muted); font-weight: 700; margin: 0; }
.card h3 { font-size: 1.35rem; margin: 2px 0 2px; }
.model { font-size: .85rem; font-weight: 500; color: var(--muted); margin-left: 6px; }
.card-desc { color: var(--muted); font-size: .9rem; }
.metric { border-top: 1px solid var(--border); padding-top: 10px; margin-top: 10px; }
.metric-name { font-size: .85rem; color: var(--muted); margin: 0; font-weight: 600; }
.metric-value { font-size: 1.45rem; font-weight: 650; margin: 0; font-variant-numeric: tabular-nums; }
.metric-value.bad { color: var(--safety); }
.metric-sub { font-size: .9rem; color: var(--muted); margin: 0; }
.rate-tag { display: inline-block; font-size: .7rem; font-weight: 700; letter-spacing: .04em; border: 1px solid var(--border);
  border-radius: 4px; padding: 0 5px; margin-left: 4px; color: var(--muted); }

.delta { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 1px; background: var(--border);
  border: 1px solid var(--border); border-radius: 12px; overflow: hidden; margin: 16px 0 8px; }
.delta > div, .stats > div { background: var(--surface); padding: 12px 16px; }
.d-name { font-size: .8rem; color: var(--muted); margin: 0; font-weight: 600; }
.d-val { font-size: 1.1rem; font-weight: 650; margin: 0; font-variant-numeric: tabular-nums; }
.rate-note, .wide-note { font-size: .85rem; color: var(--muted); }
.hint { color: var(--muted); font-size: .9rem; }

.changed { margin-top: 24px; }
.lists { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }
.worse-h, .better-h { margin-bottom: 8px; }
.change-list { list-style: none; padding: 0; margin: 0; display: grid; gap: 8px; }
.change-list .none { color: var(--muted); padding: 10px 0; }
.change details { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; }
.change summary { list-style: none; cursor: pointer; display: flex; gap: 10px; align-items: flex-start; padding: 12px 14px; }
.change summary::-webkit-details-marker { display: none; }
.marker { margin-left: auto; color: var(--muted); font-weight: 700; flex: 0 0 auto; }
.marker::before { content: "+"; }
.change details[open] .marker::before { content: "-"; }
.dot { flex: 0 0 auto; width: 10px; height: 10px; border-radius: 50%; margin-top: .45em; }
.worse .dot.sev-safety { background: var(--safety); }
.worse .dot.sev-quality { background: var(--quality); }
.better .dot { background: var(--ship-edge); }
.more { font-size: .8rem; color: var(--muted); white-space: nowrap; }
.change-body { padding: 0 14px 14px; border-top: 1px solid var(--border); }
.context { margin-top: 10px; }
.origin { font-size: .9rem; background: var(--surface-2); border-radius: 6px; padding: 6px 10px; }
.also { font-size: .9rem; color: var(--muted); }
.evidence { background: var(--surface-2); border-radius: 8px; padding: 10px 12px; margin: 8px 0; }
.ev-title { font-weight: 600; margin-bottom: 4px; }
.evidence dl { margin: 0; display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; font-size: .9rem; }
.evidence dt { color: var(--muted); }
.evidence dd { margin: 0; overflow-wrap: anywhere; }
.sev { font-size: .7rem; font-weight: 700; text-transform: uppercase; letter-spacing: .06em; border-radius: 4px; padding: 1px 6px; border: 1px solid; }
.sev-safety { color: var(--safety); }
.sev-quality { color: var(--quality); }
.two { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; margin-top: 8px; }
.col-h { font-weight: 600; font-size: .85rem; color: var(--muted); margin: 0 0 4px; }
.steps { list-style: none; margin: 0; padding: 0; font-size: .88rem; }
.steps li { display: flex; gap: 8px; padding: 3px 6px; border-radius: 6px; }
.steps li.flag { background: var(--flag-bg); box-shadow: inset 3px 0 0 var(--safety); }
.steps li.final { color: var(--muted); }
.step-n { flex: 0 0 2.2em; color: var(--muted); font-variant-numeric: tabular-nums; }
.flag-note { font-weight: 700; color: var(--safety); white-space: nowrap; }
.trace-link { margin: 10px 0 0; }
.linkish { background: none; border: 0; padding: 0; color: var(--accent); text-decoration: underline; cursor: pointer; }

.decide { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px; margin-top: 24px; }
.btns { display: flex; gap: 10px; flex-wrap: wrap; margin: 12px 0 4px; }
.btn { border: 1px solid var(--border); background: var(--surface-2); border-radius: 8px; padding: 10px 16px; cursor: pointer;
  font-weight: 600; min-height: 44px; }
.btn-primary { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); }
.btn[disabled] { background: var(--surface-2); color: var(--muted); border: 1px dashed var(--border); cursor: not-allowed; }
.btn-small { padding: 6px 12px; min-height: 36px; font-size: .9rem; }
.why { font-size: .9rem; color: var(--muted); }
.cmd { margin-top: 12px; }
.cmd pre { background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px; padding: 12px; white-space: pre-wrap;
  overflow-wrap: anywhere; margin: 6px 0 8px; }

.table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 12px; background: var(--surface); }
table { border-collapse: collapse; width: 100%; font-size: .9rem; }
th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--border); vertical-align: top; }
th { font-size: .8rem; color: var(--muted); font-weight: 650; background: var(--surface-2); }
tbody tr:last-child td { border-bottom: 0; }
td.num { font-variant-numeric: tabular-nums; }
td.bad { color: var(--safety); font-weight: 700; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 1px; background: var(--border);
  border: 1px solid var(--border); border-radius: 12px; overflow: hidden; margin-bottom: 12px; }

.trace-controls { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }
.trace-controls label { display: flex; gap: 8px; align-items: center; font-weight: 600; max-width: 100%; }
.trace-controls select { font: inherit; padding: 8px 10px; border-radius: 8px; border: 1px solid var(--border);
  background: var(--surface); color: var(--text); min-height: 44px; max-width: 100%; min-width: 0; }
.seg { display: inline-flex; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.seg-btn { background: var(--surface); border: 0; padding: 8px 12px; cursor: pointer; min-height: 44px; }
.seg-btn + .seg-btn { border-left: 1px solid var(--border); }
.seg-btn[aria-pressed="true"] { background: var(--accent); color: var(--accent-ink); font-weight: 600; }
.trace-out .case-head { margin-bottom: 10px; }
.trace-out h3:focus { outline: none; }
.trace-out h3:focus-visible { outline: 3px solid var(--focus); }
.check-list { list-style: none; padding: 0; margin: 0 0 14px; display: grid; gap: 4px; font-size: .9rem; }
.check-list li { display: flex; gap: 8px; align-items: baseline; }
.status { font-size: .7rem; font-weight: 700; text-transform: uppercase; border-radius: 4px; padding: 1px 6px; flex: 0 0 auto; }
.status-pass { background: var(--ship-badge); color: var(--ship-badge-ink); }
.status-fail { background: var(--block-badge); color: var(--block-badge-ink); }
.status-na { background: var(--surface-2); color: var(--muted); border: 1px solid var(--border); }
.trace-steps { display: grid; gap: 8px; }
.tstep { border: 1px solid var(--border); border-radius: 10px; background: var(--surface); padding: 10px 12px; }
.tstep.flag { border-color: var(--safety); box-shadow: inset 4px 0 0 var(--safety); background: var(--flag-bg); }
.tstep-head { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
.tstep-head code { font-weight: 700; }
.tstep pre { margin: 6px 0 0; background: var(--surface-2); border-radius: 6px; padding: 8px; white-space: pre-wrap; overflow-wrap: anywhere; max-height: 260px; overflow-y: auto; }
.kv { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 8px; }
.kv p { margin: 6px 0 0; font-size: .75rem; font-weight: 700; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }

.foot { border-top: 1px solid var(--border); padding-top: 16px; padding-bottom: 32px; font-size: .9rem; }
@media (max-width: 520px) {
  h1 { font-size: 1.4rem; }
  .headline { font-size: 1.05rem; }
  .verdict { padding: 16px; }
  .card, .decide { padding: 16px; }
  .lists { grid-template-columns: 1fr; }
  .evidence dl { grid-template-columns: 1fr; gap: 0 12px; }
  .evidence dd { margin-bottom: 6px; }
}
@media print { .tabs, .switch, .btns, .trace { display: none; } .view[hidden], section.cmp[hidden] { display: block !important; } }
"""


JS = """
(function () {
  var data = JSON.parse(document.getElementById('shipcheck-data').textContent);
  var doc = data.doc, runs = data.runs;
  var checksById = {};
  doc.checks.forEach(function (c) { checksById[c.id] = c; });

  function el(tag, attrs, kids) {
    var n = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === 'text') n.textContent = attrs[k]; else n.setAttribute(k, attrs[k]);
    });
    (kids || []).forEach(function (k) { if (k) n.appendChild(typeof k === 'string' ? document.createTextNode(k) : k); });
    return n;
  }

  // Comparison switch: pressed-state buttons, remembered in the URL hash so a link opens the same view.
  var switches = document.querySelectorAll('.switch-btn');
  function showComparison(id) {
    var found = false;
    document.querySelectorAll('section.cmp').forEach(function (s) {
      var on = s.getAttribute('data-cmp') === id;
      if (on) found = true;
      s.hidden = !on;
    });
    if (!found) return false;
    switches.forEach(function (b) { b.setAttribute('aria-pressed', b.getAttribute('data-cmp') === id ? 'true' : 'false'); });
    return true;
  }
  switches.forEach(function (b) {
    b.addEventListener('click', function () {
      var id = b.getAttribute('data-cmp');
      showComparison(id);
      try { history.replaceState(null, '', '#' + id); } catch (err) {}
    });
  });
  if (location.hash) showComparison(location.hash.slice(1));

  document.querySelectorAll('section.cmp').forEach(function (sec) {
    // Summary and Engineer view: tabs with arrow-key support.
    var tabs = Array.prototype.slice.call(sec.querySelectorAll('.tab'));
    function showView(name, focus) {
      tabs.forEach(function (t) {
        var on = t.getAttribute('data-view') === name;
        t.setAttribute('aria-selected', on ? 'true' : 'false');
        t.setAttribute('tabindex', on ? '0' : '-1');
        if (on && focus) t.focus();
      });
      sec.querySelectorAll('.view').forEach(function (v) { v.hidden = v.getAttribute('data-view') !== name; });
    }
    tabs.forEach(function (t, i) {
      t.addEventListener('click', function () { showView(t.getAttribute('data-view'), false); });
      t.addEventListener('keydown', function (ev) {
        var next = null;
        if (ev.key === 'ArrowRight') next = tabs[(i + 1) % tabs.length];
        if (ev.key === 'ArrowLeft') next = tabs[(i - 1 + tabs.length) % tabs.length];
        if (ev.key === 'Home') next = tabs[0];
        if (ev.key === 'End') next = tabs[tabs.length - 1];
        if (next) { ev.preventDefault(); showView(next.getAttribute('data-view'), true); }
      });
    });

    // Approve and roll back only reveal the CLI command. A static file cannot record anything.
    var box = sec.querySelector('.cmd');
    var cmdStatus = sec.querySelector('.cmd-status');
    sec.querySelectorAll('.btns .btn[data-cmd]').forEach(function (b) {
      b.addEventListener('click', function () {
        box.hidden = false;
        box.querySelector('code').textContent = b.getAttribute('data-cmd');
        cmdStatus.textContent = 'Command shown below the buttons.';
      });
    });
    box.querySelector('.copy').addEventListener('click', function () {
      var code = box.querySelector('code');
      function selectCode() {
        var r = document.createRange(); r.selectNodeContents(code);
        var s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
        cmdStatus.textContent = 'Selected. Press Ctrl+C or Cmd+C to copy.';
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(code.textContent).then(function () { cmdStatus.textContent = 'Copied.'; }, selectCode);
      } else { selectCode(); }
    });

    // Trace viewer
    var trace = sec.querySelector('.trace');
    var select = trace.querySelector('.trace-case');
    var out = trace.querySelector('.trace-out');
    var status = trace.querySelector('.trace-status');
    var segs = trace.querySelectorAll('.seg-btn');
    var version = sec.getAttribute('data-candidate');
    function render() {
      var cid = select.value;
      var run = runs[version][cid];
      var res = doc.results[version][cid];
      var c = doc.cases[cid];
      var failing = {};
      res.checks.forEach(function (ch) { if (ch.status === 'fail') failing[ch.step === null ? 'end' : ch.step] = true; });
      var verdictText = cid + ' on ' + version + ': ' + (res.passed ? 'passed' : 'failed');
      out.textContent = '';
      out.appendChild(el('div', {'class': 'case-head'}, [
        el('h3', {text: verdictText, tabindex: '-1'}),
        el('p', {'class': 'muted', text: c.specialty + ', plan ' + c.insurance_plan + '. ' + c.referral_reason + '. Final action: ' + run.final.action + '.'})
      ]));
      var list = el('ul', {'class': 'check-list'});
      res.checks.forEach(function (ch) {
        var cls = ch.status === 'pass' ? 'status-pass' : (ch.status === 'fail' ? 'status-fail' : 'status-na');
        var where = (ch.step === null || ch.actual.indexOf('step ') !== -1) ? '' : ' (at step ' + (ch.step + 1) + ')';
        list.appendChild(el('li', null, [
          el('span', {'class': 'status ' + cls, text: ch.status}),
          el('span', null, [el('strong', {text: checksById[ch.check_id].plain_name + '. '}), ch.actual + where])
        ]));
      });
      out.appendChild(list);
      var steps = el('div', {'class': 'trace-steps'});
      run.steps.forEach(function (st, i) {
        steps.appendChild(el('div', {'class': 'tstep' + (failing[i] ? ' flag' : '')}, [
          el('div', {'class': 'tstep-head'}, [
            el('span', {'class': 'muted', text: 'Step ' + (i + 1)}), el('code', {text: st.tool}),
            el('span', {text: st.plain}), failing[i] ? el('span', {'class': 'flag-note', text: 'Failing step'}) : null
          ]),
          el('div', {'class': 'kv'}, [
            el('div', null, [el('p', {text: 'args'}), el('pre', {text: JSON.stringify(st.args, null, 2)})]),
            el('div', null, [el('p', {text: 'result'}), el('pre', {text: JSON.stringify(st.result, null, 2)})])
          ])
        ]));
      });
      steps.appendChild(el('div', {'class': 'tstep' + (failing.end ? ' flag' : '')}, [
        el('div', {'class': 'tstep-head'}, [el('span', {'class': 'muted', text: 'Final'}), el('code', {text: run.final.action}),
          el('span', {text: run.usage.input_tokens.toLocaleString('en-US') + ' input and ' + run.usage.output_tokens.toLocaleString('en-US') + ' output tokens'}),
          failing.end ? el('span', {'class': 'flag-note', text: 'Failing step'}) : null]),
        el('pre', {text: JSON.stringify(run.final.packet, null, 2)})
      ]));
      out.appendChild(steps);
      status.textContent = 'Showing ' + verdictText + '.';
    }
    select.value = out.getAttribute('data-first');
    select.addEventListener('change', render);
    segs.forEach(function (b) {
      b.addEventListener('click', function () {
        version = b.getAttribute('data-version');
        segs.forEach(function (x) { x.setAttribute('aria-pressed', x === b ? 'true' : 'false'); });
        render();
      });
    });
    sec.querySelectorAll('[data-trace]').forEach(function (b) {
      b.addEventListener('click', function () {
        select.value = b.getAttribute('data-trace');
        showView('engineer', false);
        render();
        var h = out.querySelector('h3');
        trace.scrollIntoView({block: 'start'});
        h.focus({preventScroll: true});
      });
    });
    render();
  });
})();
"""
