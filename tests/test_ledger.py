import itertools
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from shipcheck import ledger

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "approvals.jsonl"
        for i, who in enumerate(("Clinic Ops Lead", "Practice Manager", "Clinic Ops Lead")):
            ledger.append(self.path, {"decision": "approved", "candidate": f"v{i + 2}", "by": who, "note": "ok"}, now=T0)

    def tearDown(self):
        self.dir.cleanup()

    def lines(self):
        return self.path.read_text().splitlines()

    def write(self, lines):
        self.path.write_text("\n".join(lines) + "\n")

    def test_append_then_verify(self):
        result = ledger.verify(self.path)
        self.assertTrue(result.ok)
        self.assertEqual(result.message, "ledger OK (3 records)")
        first = json.loads(self.lines()[0])
        self.assertEqual(first["prev_hash"], ledger.GENESIS)
        self.assertEqual(first["seq"], 1)
        self.assertEqual(first["timestamp"], "2026-10-01T12:00:00Z")

    def test_each_record_hashes_the_one_before(self):
        recs = [json.loads(x) for x in self.lines()]
        for prev, cur in itertools.pairwise(recs):
            self.assertEqual(cur["prev_hash"], prev["hash"])

    def test_edited_record_is_detected(self):
        lines = self.lines()
        rec = json.loads(lines[1])
        rec["by"] = "Someone Else"
        lines[1] = json.dumps(rec)
        self.write(lines)
        result = ledger.verify(self.path)
        self.assertFalse(result.ok)
        self.assertEqual(result.broken_at, 2)
        self.assertTrue(result.message.startswith("ledger BROKEN at record 2"))

    def test_reordered_records_are_detected(self):
        lines = self.lines()
        lines[1], lines[2] = lines[2], lines[1]
        self.write(lines)
        result = ledger.verify(self.path)
        self.assertFalse(result.ok)
        self.assertEqual(result.broken_at, 2)

    def test_deleting_a_middle_record_is_detected(self):
        lines = self.lines()
        del lines[1]
        self.write(lines)
        self.assertFalse(ledger.verify(self.path).ok)

    def test_deleting_trailing_records_needs_the_head_hash(self):
        # Documented limit: a truncated chain is still a valid chain.
        head = json.loads(self.lines()[-1])["hash"]
        self.write(self.lines()[:-1])
        self.assertTrue(ledger.verify(self.path).ok)
        caught = ledger.verify(self.path, expected_head=head)
        self.assertFalse(caught.ok)
        self.assertIn("stored head", caught.message)

    def test_refuses_to_extend_a_broken_ledger(self):
        lines = self.lines()
        lines[0] = lines[0].replace("Clinic Ops Lead", "Clinic Ops Leed")
        self.write(lines)
        with self.assertRaises(ledger.LedgerError):
            ledger.append(self.path, {"decision": "approved", "candidate": "v9", "by": "x", "note": "y"})
        self.assertEqual(len(self.lines()), 3)

    def test_garbage_line_is_reported_not_crashed_on(self):
        self.write(self.lines() + ["not json"])
        result = ledger.verify(self.path)
        self.assertFalse(result.ok)
        self.assertEqual(result.broken_at, 4)

    def test_missing_ledger_verifies_as_empty(self):
        result = ledger.verify(Path(self.dir.name) / "none.jsonl")
        self.assertTrue(result.ok)
        self.assertEqual(result.message, "ledger OK (0 records)")


if __name__ == "__main__":
    unittest.main()


class LedgerReviewFindingsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "approvals.jsonl"

    def tearDown(self):
        self.dir.cleanup()

    def test_hash_matches_the_documented_formula(self):
        rec = ledger.append(self.path, {"decision": "approved", "candidate": "v3", "by": "A", "note": "n"}, now=T0)
        body = {k: v for k, v in rec.items() if k != "hash"}
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        import hashlib

        self.assertEqual(rec["hash"], hashlib.sha256((ledger.GENESIS + canonical).encode("utf-8")).hexdigest())

    def test_entry_cannot_override_chain_fields(self):
        rec = ledger.append(self.path, {"candidate": "v3", "seq": 99, "hash": "x", "prev_hash": "y"}, now=T0)
        self.assertEqual((rec["seq"], rec["prev_hash"]), (1, ledger.GENESIS))
        self.assertTrue(ledger.verify(self.path).ok)

    def test_append_after_a_missing_final_newline_keeps_the_chain(self):
        for i in range(2):
            ledger.append(self.path, {"candidate": f"v{i}", "by": "A", "note": "n"}, now=T0)
        self.path.write_text(self.path.read_text().rstrip("\n"))
        ledger.append(self.path, {"candidate": "v9", "by": "A", "note": "n"}, now=T0)
        result = ledger.verify(self.path)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.count, 3)

    def test_broken_record_number_ignores_blank_lines(self):
        for i in range(3):
            ledger.append(self.path, {"candidate": f"v{i}", "by": "A", "note": "n"}, now=T0)
        lines = self.path.read_text().splitlines()
        self.path.write_text("\n\n".join(lines + ["garbage"]) + "\n")
        result = ledger.verify(self.path)
        self.assertEqual(result.broken_at, 4)
        self.assertIn("record 4 (line 7)", result.message)
