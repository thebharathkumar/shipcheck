"""The README and DEMO.md may only quote numbers that examples/results.json produces."""

import json
import re
import subprocess
import unittest
from pathlib import Path

from shipcheck import ledger
from shipcheck.gate import change_text, correct_text, money_text, p_text, pct

ROOT = Path(__file__).resolve().parent.parent
RESULTS = json.loads((ROOT / "examples" / "results.json").read_text(encoding="utf-8"))
README = (ROOT / "README.md").read_text(encoding="utf-8")
DEMO = (ROOT / "DEMO.md").read_text(encoding="utf-8")
LEDGER = ROOT / "examples" / "approvals.jsonl"

# The Wilson interval is a 95% interval. That one percentage is a setting, not a result.
CONFIDENCE_LEVEL = "95%"


def results_table(doc: dict) -> str:
    """The README's results table, rebuilt from results.json."""
    versions = doc["versions"]
    cmps = {c["candidate"]: c for c in doc["comparisons"]}
    base = doc["comparisons"][0]["baseline"]
    names = [base] + [c["candidate"] for c in doc["comparisons"]]
    label = doc["rate_card"]["label"]

    def row(title, cells):
        return "| " + title + " | " + " | ".join(cells) + " |"

    def per_version(fn):
        return [fn(n, versions[n], cmps.get(n)) for n in names]

    lines = [
        row("", [f"{n} ({versions[n]['model_label']})" for n in names]),
        "|---|" + "---|" * len(names),
        row("Verdict against " + base, per_version(lambda n, v, c: c["verdict"]["level"] if c else "current")),
        row("Correct", per_version(lambda n, v, c: correct_text(v["n_passed"], v["n_cases"]))),
        row("Likely range (Wilson 95%)", per_version(lambda n, v, c: f"{pct(v['interval'][0])} to {pct(v['interval'][1])}")),
        row("Safety issues", per_version(lambda n, v, c: str(v["safety_issues"]))),
        row("Cases fixed / broken vs " + base,
            per_version(lambda n, v, c: f"{c['discordant']['fixed']} / {c['discordant']['broken']}" if c else "")),
        row("McNemar exact", per_version(lambda n, v, c: p_text(c["p_value"]) if c else "")),
        row("Tokens per run", per_version(
            lambda n, v, c: f"{v['tokens']['total_per_run']:,.0f}" + (f" ({change_text(c['cost_delta']['tokens_change'])})" if c else ""))),
        row(f"Cost per 1,000 runs at {label}", per_version(
            lambda n, v, c: money_text(v["cost"]["per_1000_runs"]) + (f" ({change_text(c['cost_delta']['cost_change'])})" if c else ""))),
    ]
    return "\n".join(lines)


def allowed(doc: dict) -> dict[str, set[str]]:
    pcts, counts, dollars, ps = {CONFIDENCE_LEVEL}, set(), set(), set()
    for v in doc["versions"].values():
        pcts |= {pct(v["pass_rate"]), pct(v["interval"][0]), pct(v["interval"][1])}
        counts.add(f"{v['n_passed']} of {v['n_cases']}")
        if v["cost"]:
            dollars.add(money_text(v["cost"]["per_1000_runs"]))
    for c in doc["comparisons"]:
        d = c["cost_delta"]
        pcts |= {pct(abs(d["tokens_change"])), pct(abs(d["cost_change"]))}
        ps.add(p_text(c["p_value"]))
    return {"pct": pcts, "count": counts, "dollar": dollars, "p": ps}


class DocsNumbersTest(unittest.TestCase):
    allowed = allowed(RESULTS)

    def scan(self, text, name):
        for m in re.findall(r"\d+(?:\.\d+)?%", text):
            self.assertIn(m, self.allowed["pct"], f"{name}: {m} is not a number from results.json")
        for m in re.findall(r"\b\d+ of \d+\b", text):
            self.assertIn(m, self.allowed["count"], f"{name}: '{m}' is not a count from results.json")
        for m in re.findall(r"\$\d[\d,]*(?:\.\d+)?", text):
            self.assertIn(m, self.allowed["dollar"], f"{name}: {m} is not a cost from results.json")
        for m in re.findall(r"p = \d\.\d+", text):
            self.assertIn(m, self.allowed["p"], f"{name}: '{m}' is not a p-value from results.json")

    def test_readme_results_table_matches_results_json(self):
        self.assertIn(results_table(RESULTS), README)

    def test_every_number_in_readme_comes_from_results_json(self):
        self.scan(README, "README.md")

    def test_every_number_in_demo_script_comes_from_results_json(self):
        self.scan(DEMO, "DEMO.md")
        v2 = RESULTS["comparisons"][0]
        self.assertIn(f"{pct(abs(v2['cost_delta']['cost_change']))} cheaper", DEMO)

    def test_readme_carries_the_key_line_and_both_verdicts(self):
        self.assertIn("An average-score dashboard would have shipped v2. shipcheck blocks it because it introduced "
                      "new safety failures, even though its average went up.", README)
        levels = {c["candidate"]: c["verdict"]["level"] for c in RESULTS["comparisons"]}
        self.assertEqual(levels, {"v2": "BLOCK", "v3": "REVIEW"})
        v2 = RESULTS["versions"]["v2"]["pass_rate"]
        v1 = RESULTS["versions"]["v1"]["pass_rate"]
        self.assertGreater(v2, v1, "the key line says v2's average went up")

    def test_prose_counts_match_the_suite(self):
        n = RESULTS["suite"]["cases"]
        for m in re.findall(r"\b(\d+) cases\b", README + DEMO):
            self.assertEqual(int(m), n, f"'{m} cases' does not match the {n}-case suite")
        flags = sum(c["origin"] == "staff_flag" for c in RESULTS["cases"].values())
        words = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six", 7: "Seven", 8: "Eight"}
        self.assertIn(f"{words[flags]} cases in this suite started as a staff flag", README)

    def test_readme_states_the_real_test_count(self):
        count = unittest.TestLoader().discover(str(ROOT / "tests"), top_level_dir=str(ROOT)).countTestCases()
        m = re.search(r"The suite has (\d+) tests\.", README)
        self.assertIsNotNone(m, "README must say 'The suite has N tests.'")
        self.assertEqual(int(m.group(1)), count)

    def test_recorded_approval_is_real_and_matches_results(self):
        self.assertTrue(ledger.verify(LEDGER).ok)
        rec = ledger.read(LEDGER)[-1]
        v3 = next(c for c in RESULTS["comparisons"] if c["candidate"] == "v3")
        self.assertEqual((rec["candidate"], rec["verdict"], rec["decision"]), ("v3", v3["verdict"]["level"], "approved"))
        self.assertEqual(rec["summary"]["cases_broken"], v3["buckets"]["broken"])
        self.assertEqual(rec["summary"]["cost_change"], v3["cost_delta"]["cost_change"])
        self.assertIn(rec["hash"], README)


def repo_files() -> list[Path]:
    """Files git would commit: tracked plus untracked, minus ignored. Falls back to a plain walk."""
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout
        return [ROOT / line for line in out.splitlines() if (ROOT / line).is_file()]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in ROOT.rglob("*") if p.is_file()
                and not any(part.startswith(".") or part == "__pycache__" or part.endswith(".egg-info")
                            for part in p.relative_to(ROOT).parts)]


class DocsHygieneTest(unittest.TestCase):
    def test_readme_says_synthetic_and_independent(self):
        self.assertIn("Independent demonstration. Not affiliated with any company. All data is synthetic.", README)
        self.assertIn("synthetic and simulated", README)
        self.assertIn("simulated", DEMO)

    def test_no_em_or_en_dashes_in_any_repo_file(self):
        for path in repo_files():
            if path.suffix.lower() in {".png", ".jpg", ".gif", ".pyc"}:
                continue
            text = path.read_text(encoding="utf-8")
            # Built at runtime so this file does not contain what it forbids.
            for bad in (chr(0x2014), chr(0x2013), "&" + "mdash;", "&" + "ndash;"):
                self.assertNotIn(bad, text, f"{path.relative_to(ROOT)} contains a dash character")


if __name__ == "__main__":
    unittest.main()
