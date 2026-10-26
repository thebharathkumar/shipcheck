"""Command line interface: demo, compare, approve, rollback, verify-ledger, checks."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from shipcheck import ledger
from shipcheck.checks import CHECKS
from shipcheck.cost import load_rate_card
from shipcheck.gate import change_text, correct_text, money_text, wide_interval_text
from shipcheck.model import BLOCK, SAFETY, load_runs, load_suite, read_json
from shipcheck.report import DEFAULT_RESULTS, build_results, write_report
from shipcheck.simulate import VERSIONS, runs_file, simulate

DEFAULT_LEDGER = "approvals.jsonl"


def _bundled(relative: str) -> Path:
    """The bundled suite and rate card live in the repo checkout, next to the package.

    An editable install (pip install -e .) finds them there. A regular install
    does not ship them, so fall back to the current folder, which works when
    running from the repo root.
    """
    for base in (Path(__file__).resolve().parent.parent, Path.cwd()):
        if (base / relative).exists():
            return base / relative
    return Path(__file__).resolve().parent.parent / relative


DEFAULT_SUITE = _bundled("suites/referral_coordinator")
DEFAULT_RATES = _bundled("rates.example.json")


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _display_path(p: Path) -> str:
    try:
        return p.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return p.resolve().as_posix()


def _print_summary(doc: dict, out=None) -> None:
    w = (out or sys.stdout).write
    rc = doc["rate_card"]
    if doc["simulated"]:
        w("shipcheck: simulated data. All cases and agent runs are synthetic.\n")
    elif doc.get("synthetic_cases"):
        w("shipcheck: synthetic test cases. No real patient data.\n")
    for cmp in doc["comparisons"]:
        v = cmp["verdict"]
        base, cand = doc["versions"][cmp["baseline"]], doc["versions"][cmp["candidate"]]
        d = cmp["cost_delta"]
        w(f"\n{cmp['title']}: {v['level']}\n")
        w(f"  {v['headline']}\n")
        for r in v["reasons"][:3]:
            w(f"  - {r}\n")
        if len(v["reasons"]) > 3:
            w(f"  ... and {len(v['reasons']) - 3} more\n")
        w(f"  Correct: {correct_text(base['n_passed'], base['n_cases'])} before, "
          f"{correct_text(cand['n_passed'], cand['n_cases'])} now. Safety issues: {base['safety_issues']} before, "
          f"{cand['safety_issues']} now.\n")
        wide = wide_interval_text(base["n_cases"], [base["interval"], cand["interval"]])
        if wide:
            w(f"  {wide}\n")
        w(f"  Tokens per run: {change_text(d['tokens_change'])}.")
        if d["cost_change"] is not None:
            cur = base["cost"]["currency"]
            w(f" Cost per 1,000 runs: {money_text(d['cost_per_1000_before'], cur)} to "
              f"{money_text(d['cost_per_1000_after'], cur)} ({change_text(d['cost_change'])}).")
        elif rc:
            missing = sorted({x["model_label"] for x in (base, cand) if not x["cost"]})
            w(f" No cost estimate: the rate card has no rate for {', '.join(missing)}.")
        w("\n")
    if rc:
        w(f"\nDollar figures use {rc['label']}. Source: {rc['source']} Date: {rc['as_of']}.\n")


def cmd_demo(args) -> int:
    started = time.perf_counter()
    out = Path(args.out)
    suite = load_suite(args.suite)
    rates = load_rate_card(args.rates)
    runs = {v: simulate(suite, v) for v in VERSIONS}
    for v in VERSIONS:
        _write_json(out / "runs" / f"{v}.json", runs_file(suite, v))
    results_path = out / "results.json"
    doc = build_results(
        suite, runs, [("v1", "v2"), ("v1", "v3")], rates,
        descriptions={v: cfg["description"] for v, cfg in VERSIONS.items()},
        simulated=True,
        results_path=_display_path(results_path),
    )
    _write_json(results_path, doc)
    write_report(out / "report.html", doc, runs)
    _print_summary(doc)
    elapsed = time.perf_counter() - started
    print(f"\nWrote {_display_path(out / 'report.html')}, {_display_path(results_path)} and "
          f"{_display_path(out / 'runs')}/ in {elapsed:.2f}s.")
    return 0


def cmd_compare(args) -> int:
    suite = load_suite(args.suite)
    rates = load_rate_card(args.rates)
    raw_b, raw_c = read_json(args.baseline), read_json(args.candidate)
    base_runs, cand_runs = load_runs(args.baseline), load_runs(args.candidate)
    if not base_runs or not cand_runs:
        print("error: a runs file is empty", file=sys.stderr)
        return 2
    b, c = base_runs[0].agent_version, cand_runs[0].agent_version
    if b == c:
        print(f"error: baseline and candidate both have agent_version {b!r}; give them different names", file=sys.stderr)
        return 2
    descriptions = {}
    for name, raw in ((b, raw_b), (c, raw_c)):
        if isinstance(raw, dict) and raw.get("description"):
            descriptions[name] = raw["description"]
    simulated = all(isinstance(r, dict) and r.get("simulated") for r in (raw_b, raw_c))
    out = Path(args.out)
    results_path = Path(args.results) if args.results else out.with_suffix(".json")
    doc = build_results(suite, {b: base_runs, c: cand_runs}, [(b, c)], rates, descriptions, simulated,
                        results_path=_display_path(results_path))
    _write_json(results_path, doc)
    write_report(out, doc, {b: base_runs, c: cand_runs})
    _print_summary(doc)
    print(f"\nWrote {_display_path(out)} and {_display_path(results_path)}.")
    if args.exit_code:
        return {"SHIP": 0, "REVIEW": 3, "BLOCK": 4}[doc["comparisons"][0]["verdict"]["level"]]
    return 0


def _find_comparison(doc: dict, candidate: str, baseline: str | None) -> dict:
    if not isinstance(doc, dict) or not isinstance(doc.get("comparisons"), list):
        raise ValueError("the results file has no comparisons; run shipcheck demo or shipcheck compare first")
    found = [c for c in doc["comparisons"] if c["candidate"] == candidate and (baseline is None or c["baseline"] == baseline)]
    if not found:
        names = ", ".join(sorted({c["candidate"] for c in doc["comparisons"]}))
        raise ValueError(f"no comparison for candidate {candidate!r} in the results file (have: {names})")
    if len(found) > 1:
        raise ValueError(f"{candidate!r} was compared with more than one baseline; pass --baseline")
    return found[0]


def _decision(args, decision: str) -> int:
    if not args.by.strip() or not args.note.strip():
        print("error: --by must name a person or role, and --note must say why", file=sys.stderr)
        return 2
    doc = read_json(args.results)
    cmp = _find_comparison(doc, args.candidate, args.baseline)
    level = cmp["verdict"]["level"]
    if decision == "approve" and level == BLOCK:
        print(f"Not recorded: {args.candidate} is BLOCKED. {cmp['verdict']['headline']}", file=sys.stderr)
        print("A blocked change cannot be approved. Fix the safety failures and run shipcheck again, "
              f"or record a rollback with: shipcheck rollback --candidate {args.candidate} --by NAME --note TEXT",
              file=sys.stderr)
        return 1
    base, cand = doc["versions"][cmp["baseline"]], doc["versions"][cmp["candidate"]]
    entry = {
        "decision": "approved" if decision == "approve" else "rolled_back",
        "candidate": cmp["candidate"],
        "baseline": cmp["baseline"],
        "verdict": level,
        "summary": {
            "headline": cmp["verdict"]["headline"],
            "baseline_correct": correct_text(base["n_passed"], base["n_cases"]),
            "candidate_correct": correct_text(cand["n_passed"], cand["n_cases"]),
            "safety_issues": {"baseline": base["safety_issues"], "candidate": cand["safety_issues"]},
            "cases_fixed": cmp["buckets"]["fixed"],
            "cases_broken": cmp["buckets"]["broken"],
            "tokens_change": cmp["cost_delta"]["tokens_change"],
            "cost_change": cmp["cost_delta"]["cost_change"],
            "simulated": doc["simulated"],
        },
        "by": args.by.strip(),
        "note": args.note.strip(),
        "results_file": Path(args.results).as_posix(),
    }
    try:
        record = ledger.append(args.ledger, entry)
    except ledger.LedgerError as err:
        print(f"Not recorded: {err}", file=sys.stderr)
        return 1
    verb = "Approved" if decision == "approve" else "Rolled back"
    print(f"{verb} {cmp['candidate']} (verdict {level}) by {entry['by']}. Record {record['seq']} in {args.ledger}.")
    print(f"Record hash: {record['hash']}")
    print("Keep this hash where the log's writer cannot edit it. verify-ledger --head HASH then also catches "
          "a rewritten log or deleted records.")
    return 0


def cmd_approve(args) -> int:
    return _decision(args, "approve")


def cmd_rollback(args) -> int:
    return _decision(args, "rollback")


def cmd_verify_ledger(args) -> int:
    if not Path(args.ledger).is_file():
        print(f"no ledger file at {args.ledger}")
        return 1
    result = ledger.verify(args.ledger, expected_head=args.head)
    print(result.message)
    return 0 if result.ok else 1


def cmd_checks(args) -> int:
    print("What shipcheck checks on every case. A case passes only if every check that applies passes.\n")
    for severity in (SAFETY, "quality"):
        print("Safety checks (any new failure blocks the change):" if severity == SAFETY
              else "Quality checks (any new failure asks a person to review):")
        for c in CHECKS:
            if c.severity == severity:
                print(f"  {c.plain_name}")
                print(f"      id: {c.id}. Fails when: {c.plain_failure_text}")
        print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shipcheck", description="Is this agent change safe to ship?")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("demo", help="simulate v1, v2 and v3, compare them, and write the report")
    d.add_argument("--out", default="examples", help="output folder (default: examples)")
    d.add_argument("--suite", default=str(DEFAULT_SUITE), help="suite folder (default: the bundled referral suite)")
    d.add_argument("--rates", default=str(DEFAULT_RATES), help="rate card (default: the bundled example rates)")
    d.set_defaults(func=cmd_demo)

    c = sub.add_parser("compare", help="compare two runs files on a suite")
    c.add_argument("--suite", required=True, help="suite folder with workflow.json and cases.json")
    c.add_argument("--baseline", required=True, help="runs JSON for the current version")
    c.add_argument("--candidate", required=True, help="runs JSON for the proposed version")
    c.add_argument("--rates", help="optional rate card JSON for a cost estimate")
    c.add_argument("--out", default="report.html", help="HTML report path (default: report.html)")
    c.add_argument("--results", help="results JSON path (default: next to the report)")
    c.add_argument("--exit-code", action="store_true", help="exit 3 on REVIEW and 4 on BLOCK, for CI")
    c.set_defaults(func=cmd_compare)

    for name, func, what in (("approve", cmd_approve, "approve"), ("rollback", cmd_rollback, "roll back")):
        a = sub.add_parser(name, help=f"record a decision to {what} a candidate in the approvals log")
        a.add_argument("--candidate", required=True)
        a.add_argument("--baseline", help="only needed if the candidate was compared with several baselines")
        a.add_argument("--by", required=True, help="who made the decision")
        a.add_argument("--note", required=True, help="why")
        a.add_argument("--results", default=DEFAULT_RESULTS, help=f"results JSON (default: {DEFAULT_RESULTS})")
        a.add_argument("--ledger", default=DEFAULT_LEDGER, help=f"approvals log (default: {DEFAULT_LEDGER})")
        a.set_defaults(func=func)

    v = sub.add_parser("verify-ledger", help="check the approvals log hash chain")
    v.add_argument("--ledger", default=DEFAULT_LEDGER)
    v.add_argument("--head", help="latest hash kept elsewhere, to also catch a rewritten log or deleted records")
    v.set_defaults(func=cmd_verify_ledger)

    k = sub.add_parser("checks", help="list every check in plain language")
    k.set_defaults(func=cmd_checks)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as err:
        print(f"error: file not found: {err.filename}", file=sys.stderr)
        return 2
    except IsADirectoryError as err:
        print(f"error: expected a file but found a folder: {err.filename}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as err:
        print(f"error: not valid JSON: {err}", file=sys.stderr)
        return 2
    except (ValueError, KeyError, TypeError, UnicodeError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
