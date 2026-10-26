import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from shipcheck import cli, ledger

from tests.helpers import ROOT


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(args))
    return code, out.getvalue(), err.getvalue()


def demo_in(folder):
    """Run `shipcheck demo --out examples` from inside folder, as a user would from the repo root."""
    folder.mkdir(parents=True, exist_ok=True)
    with contextlib.chdir(folder):
        result = run_cli("demo", "--out", "examples")
    return (*result, folder / "examples")


class DemoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.code, cls.stdout, _, cls.out = demo_in(Path(cls.tmp.name) / "first")
        cls.results = json.loads((cls.out / "results.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_demo_writes_report_results_and_runs(self):
        self.assertEqual(self.code, 0)
        for name in ("report.html", "results.json", "runs/v1.json", "runs/v2.json", "runs/v3.json"):
            self.assertTrue((self.out / name).is_file(), name)

    def test_demo_verdicts_are_block_then_review(self):
        levels = {c["candidate"]: c["verdict"]["level"] for c in self.results["comparisons"]}
        self.assertEqual(levels, {"v2": "BLOCK", "v3": "REVIEW"})

    def test_terminal_summary_says_simulated_and_names_both_verdicts(self):
        self.assertIn("simulated data", self.stdout)
        self.assertIn("v2 vs v1: BLOCK", self.stdout)
        self.assertIn("v3 vs v1: REVIEW", self.stdout)
        self.assertIn("EXAMPLE RATES, NOT REAL PRICES", self.stdout)

    def test_demo_output_is_byte_identical_across_runs(self):
        *_, again = demo_in(Path(self.tmp.name) / "second")
        for name in ("report.html", "results.json", "runs/v2.json"):
            self.assertEqual((again / name).read_bytes(), (self.out / name).read_bytes(), name)

    def test_committed_examples_match_a_fresh_demo(self):
        for name in ("results.json", "report.html", "runs/v1.json", "runs/v2.json", "runs/v3.json"):
            committed = ROOT / "examples" / name
            self.assertTrue(committed.is_file(), f"run `shipcheck demo` to create examples/{name}")
            self.assertEqual(committed.read_bytes(), (self.out / name).read_bytes(),
                             f"examples/{name} is stale: run `shipcheck demo`")

    def test_demo_finishes_in_under_two_seconds_from_a_cold_start(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        start = time.perf_counter()
        proc = subprocess.run([sys.executable, "-m", "shipcheck", "demo", "--out", str(Path(self.tmp.name) / "timed")],
                              capture_output=True, text=True, env=env, cwd=self.tmp.name)
        elapsed = time.perf_counter() - start
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 2.0)


class CompareCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def compare(self, candidate, *extra):
        return run_cli("compare", "--suite", str(ROOT / "suites" / "referral_coordinator"),
                       "--baseline", str(ROOT / "examples" / "runs" / "v1.json"),
                       "--candidate", str(ROOT / "examples" / "runs" / f"{candidate}.json"),
                       "--rates", str(ROOT / "rates.example.json"),
                       "--out", str(self.dir / "report.html"), *extra)

    def test_compare_reads_plain_runs_files(self):
        code, stdout, _ = self.compare("v2")
        self.assertEqual(code, 0)
        self.assertIn("v2 vs v1: BLOCK", stdout)
        doc = json.loads((self.dir / "report.json").read_text())
        self.assertEqual(doc["comparisons"][0]["verdict"]["level"], "BLOCK")
        self.assertTrue((self.dir / "report.html").is_file())

    def test_exit_code_flag_for_ci(self):
        self.assertEqual(self.compare("v2", "--exit-code")[0], 4)
        self.assertEqual(self.compare("v3", "--exit-code")[0], 3)

    def test_runs_file_missing_cases_is_an_error(self):
        runs = json.loads((ROOT / "examples" / "runs" / "v2.json").read_text())
        runs["runs"] = runs["runs"][:-1]
        short = self.dir / "short.json"
        short.write_text(json.dumps(runs))
        code, _, err = run_cli("compare", "--suite", str(ROOT / "suites" / "referral_coordinator"),
                               "--baseline", str(ROOT / "examples" / "runs" / "v1.json"),
                               "--candidate", str(short), "--out", str(self.dir / "r.html"))
        self.assertEqual(code, 2)
        self.assertIn("missing runs for R-040", err)


class ApprovalCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = str(Path(self.tmp.name) / "approvals.jsonl")
        self.results = str(ROOT / "examples" / "results.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_approve_review_candidate_then_verify(self):
        code, out, _ = run_cli("approve", "--candidate", "v3", "--by", "Clinic Ops Lead", "--note", "Reviewed R-031",
                               "--results", self.results, "--ledger", self.ledger)
        self.assertEqual(code, 0)
        self.assertIn("Approved v3 (verdict REVIEW)", out)
        rec = ledger.read(self.ledger)[0]
        self.assertEqual((rec["decision"], rec["candidate"], rec["verdict"], rec["by"]),
                         ("approved", "v3", "REVIEW", "Clinic Ops Lead"))
        self.assertEqual(rec["summary"]["cases_broken"], ["R-031"])
        code, out, _ = run_cli("verify-ledger", "--ledger", self.ledger)
        self.assertEqual((code, out.strip()), (0, "ledger OK (1 record)"))

    def test_blocked_candidate_cannot_be_approved_but_can_be_rolled_back(self):
        code, _, err = run_cli("approve", "--candidate", "v2", "--by", "x", "--note", "y",
                               "--results", self.results, "--ledger", self.ledger)
        self.assertEqual(code, 1)
        self.assertIn("BLOCKED", err)
        self.assertEqual(ledger.read(self.ledger), [])
        code, out, _ = run_cli("rollback", "--candidate", "v2", "--by", "Clinic Ops Lead", "--note", "Keep v1",
                               "--results", self.results, "--ledger", self.ledger)
        self.assertEqual(code, 0)
        self.assertEqual(ledger.read(self.ledger)[0]["decision"], "rolled_back")

    def test_verify_ledger_reports_tampering(self):
        run_cli("approve", "--candidate", "v3", "--by", "A", "--note", "n", "--results", self.results, "--ledger", self.ledger)
        p = Path(self.ledger)
        p.write_text(p.read_text().replace('"by":"A"', '"by":"B"'))
        code, out, _ = run_cli("verify-ledger", "--ledger", self.ledger)
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("ledger BROKEN at record 1"))


class ChecksCommandTest(unittest.TestCase):
    def test_lists_every_check_in_plain_language(self):
        from shipcheck.checks import CHECKS

        code, out, _ = run_cli("checks")
        self.assertEqual(code, 0)
        for c in CHECKS:
            self.assertIn(c.plain_name, out)


if __name__ == "__main__":
    unittest.main()


class CliReviewFindingsTest(unittest.TestCase):
    """CLI behaviour found by an adversarial review, including mutants the first test suite let survive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.suite = str(ROOT / "suites" / "referral_coordinator")
        self.runs = {v: json.loads((ROOT / "examples" / "runs" / f"{v}.json").read_text()) for v in ("v1", "v2", "v3")}

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, obj):
        path = self.dir / name
        path.write_text(json.dumps(obj) if not isinstance(obj, str) else obj)
        return str(path)

    def relabel(self, version, new):
        runs = [dict(r, agent_version=new) for r in self.runs[version]["runs"]]
        return {"agent_version": new, "runs": runs}

    def compare(self, base, cand, *extra):
        return run_cli("compare", "--suite", self.suite, "--baseline", base, "--candidate", cand,
                       "--out", str(self.dir / "r.html"), *extra)

    def test_same_agent_version_on_both_sides_is_an_error(self):
        v1 = self.write("v1.json", self.runs["v1"])
        code, _, err = self.compare(v1, self.write("v2.json", self.relabel("v2", "v1")))
        self.assertEqual(code, 2)
        self.assertIn("both have agent_version", err)

    def test_plain_list_runs_files_work_and_the_page_still_says_synthetic(self):
        b = self.write("b.json", self.runs["v1"]["runs"])
        c = self.write("c.json", self.runs["v2"]["runs"])
        code, out, _ = self.compare(b, c)
        self.assertEqual(code, 0)
        self.assertIn("v2 vs v1: BLOCK", out)
        self.assertIn("synthetic test cases", out)
        doc = json.loads((self.dir / "r.json").read_text())
        self.assertFalse(doc["simulated"])
        self.assertIn("The test cases are synthetic.", (self.dir / "r.html").read_text())

    def test_ship_exits_zero_with_exit_code(self):
        code, out, _ = self.compare(self.write("v1.json", self.runs["v1"]),
                                    self.write("v1b.json", self.relabel("v1", "v1b")), "--exit-code")
        self.assertEqual(code, 0)
        self.assertIn("v1b vs v1: SHIP", out)

    def test_malformed_inputs_print_an_error_not_a_traceback(self):
        v1 = self.write("v1.json", self.runs["v1"])
        bad_action = json.loads(json.dumps(self.runs["v2"]))
        bad_action["runs"][0]["final"]["action"] = "faxed"
        cases = [
            ("compare", self.write("noruns.json", {"items": []})),
            ("compare", self.write("action.json", bad_action)),
            ("compare", self.write("truncated.json", '{"runs": [')),
        ]
        for _, cand in cases:
            code, _, err = self.compare(v1, cand)
            self.assertEqual(code, 2, cand)
            self.assertTrue(err.startswith("error:"), err)
        for results in (self.write("t.json", '{"compar'), self.write("l.json", [])):
            code, _, err = run_cli("approve", "--candidate", "v3", "--by", "A", "--note", "n",
                                   "--results", results, "--ledger", str(self.dir / "a.jsonl"))
            self.assertEqual(code, 2)
            self.assertTrue(err.startswith("error:"), err)
        code, _, err = run_cli("approve", "--candidate", "v3", "--by", "A", "--note", "n",
                               "--results", str(ROOT / "examples" / "results.json"), "--ledger", str(self.dir))
        self.assertEqual(code, 2)

    def test_rate_card_missing_a_field_is_a_clear_error(self):
        rates = self.write("rates.json", {"models": {"large-model": {"input": 5}}})
        code, _, err = self.compare(self.write("v1.json", self.runs["v1"]), self.write("v2.json", self.runs["v2"]),
                                    "--rates", rates)
        self.assertEqual(code, 2)
        self.assertIn("needs numeric 'input' and 'output'", err)

    def test_terminal_uses_the_rate_card_currency(self):
        rates = json.loads((ROOT / "rates.example.json").read_text())
        rates["currency"] = "EUR"
        code, out, _ = self.compare(self.write("v1.json", self.runs["v1"]), self.write("v2.json", self.runs["v2"]),
                                    "--rates", self.write("eur.json", rates))
        self.assertEqual(code, 0)
        self.assertIn("98.14 EUR to 29.13 EUR", out)
        self.assertNotIn("$", out)

    def test_ambiguous_baseline_must_be_named(self):
        from shipcheck.model import load_suite, Run
        from shipcheck.report import build_results

        suite = load_suite(self.suite)
        runs = {v: [Run.from_dict(r) for r in self.runs[v]["runs"]] for v in ("v1", "v2", "v3")}
        results = self.write("multi.json", build_results(suite, runs, [("v2", "v3"), ("v1", "v3")]))
        ledger_path = str(self.dir / "a.jsonl")
        code, _, err = run_cli("approve", "--candidate", "v3", "--by", "A", "--note", "n", "--results", results,
                               "--ledger", ledger_path)
        self.assertEqual(code, 2)
        self.assertIn("more than one baseline", err)
        code, _, _ = run_cli("approve", "--candidate", "v3", "--baseline", "v1", "--by", "A", "--note", "n",
                             "--results", results, "--ledger", ledger_path)
        self.assertEqual(code, 0)
        rec = ledger.read(ledger_path)[0]
        self.assertEqual((rec["baseline"], rec["verdict"], rec["note"]), ("v1", "REVIEW", "n"))

    def test_head_flag_catches_deleted_records(self):
        ledger_path = self.dir / "a.jsonl"
        results = str(ROOT / "examples" / "results.json")
        for note in ("first", "second"):
            run_cli("approve", "--candidate", "v3", "--by", "A", "--note", note, "--results", results,
                    "--ledger", str(ledger_path))
        head = ledger.read(ledger_path)[-1]["hash"]
        ledger_path.write_text(ledger_path.read_text().splitlines()[0] + "\n")
        code, out, _ = run_cli("verify-ledger", "--ledger", str(ledger_path))
        self.assertEqual(code, 0)
        code, out, _ = run_cli("verify-ledger", "--ledger", str(ledger_path), "--head", head)
        self.assertEqual(code, 1)
        self.assertIn("stored head", out)

    def test_missing_ledger_file_is_not_ok(self):
        code, out, _ = run_cli("verify-ledger", "--ledger", str(self.dir / "aprovals.jsonl"))
        self.assertEqual(code, 1)
        self.assertIn("no ledger file", out)

    def test_empty_approver_or_note_is_refused(self):
        code, _, err = run_cli("approve", "--candidate", "v3", "--by", " ", "--note", "n",
                               "--results", str(ROOT / "examples" / "results.json"), "--ledger", str(self.dir / "a.jsonl"))
        self.assertEqual(code, 2)
        self.assertFalse((self.dir / "a.jsonl").exists())


class CliSecondReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_compare_refuses_to_overwrite_an_input(self):
        import shutil

        for v in ("v1", "v3"):
            shutil.copy(ROOT / "examples" / "runs" / f"{v}.json", self.dir / f"{v}.json")
        before = (self.dir / "v3.json").read_bytes()
        code, _, err = run_cli("compare", "--suite", str(ROOT / "suites" / "referral_coordinator"),
                               "--baseline", str(self.dir / "v1.json"), "--candidate", str(self.dir / "v3.json"),
                               "--out", str(self.dir / "v3.html"))
        self.assertEqual(code, 2)
        self.assertIn("would overwrite an input", err)
        self.assertEqual((self.dir / "v3.json").read_bytes(), before)

    def test_wrapped_simulated_file_against_a_plain_list_is_not_called_simulated(self):
        wrapped = ROOT / "examples" / "runs" / "v1.json"
        plain = self.dir / "v2.json"
        plain.write_text(json.dumps(json.loads((ROOT / "examples" / "runs" / "v2.json").read_text())["runs"]))
        code, _, _ = run_cli("compare", "--suite", str(ROOT / "suites" / "referral_coordinator"),
                             "--baseline", str(wrapped), "--candidate", str(plain), "--out", str(self.dir / "r.html"))
        self.assertEqual(code, 0)
        self.assertFalse(json.loads((self.dir / "r.json").read_text())["simulated"])
