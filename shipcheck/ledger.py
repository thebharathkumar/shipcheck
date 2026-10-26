"""Append-only approvals log with a hash chain.

Each line of the JSONL file records one decision. Every record stores the
hash of the record before it, and its own hash is

    sha256(prev_hash + canonical_json(record without its hash))

so editing a field by hand, or reordering lines, breaks the chain at that
point and verify() names the first broken record.

This is tamper-evident, not tamper-proof. The hash has no secret key, so
anyone who can write the file can rewrite a record and recompute every hash
after it, and deleting the newest records leaves a valid shorter chain. Both
are caught only by keeping the latest hash somewhere the log's writer cannot
edit (a ticket, a commit, an email) and passing it to verify(expected_head=...).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def record_hash(prev_hash: str, body: dict) -> str:
    return hashlib.sha256((prev_hash + canonical_json(body)).encode("utf-8")).hexdigest()


class LedgerError(Exception):
    def __init__(self, record: int, problem: str):
        super().__init__(problem)
        self.record = record
        self.problem = problem


def read(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    records = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as e:
            n = len(records) + 1
            raise LedgerError(n, f"record {n} (line {i}) is not valid JSON ({e.msg})") from None
        if not isinstance(record, dict):
            n = len(records) + 1
            raise LedgerError(n, f"record {n} (line {i}) is not a JSON object")
        records.append(record)
    return records


@dataclass(frozen=True)
class Verification:
    ok: bool
    count: int
    broken_at: int | None = None
    problem: str = ""

    @property
    def message(self) -> str:
        if self.ok:
            return f"ledger OK ({self.count} record{'' if self.count == 1 else 's'})"
        return f"ledger BROKEN at record {self.broken_at}: {self.problem}"


def verify(path: str | Path, expected_head: str | None = None) -> Verification:
    try:
        records = read(path)
    except LedgerError as e:
        return Verification(False, 0, e.record, e.problem)
    prev = GENESIS
    for i, rec in enumerate(records, start=1):
        body = {k: v for k, v in rec.items() if k != "hash"}
        if rec.get("seq") != i:
            return Verification(False, len(records), i, f"expected seq {i}, found {rec.get('seq')}")
        if rec.get("prev_hash") != prev:
            return Verification(False, len(records), i, "prev_hash does not match the record before it")
        if rec.get("hash") != record_hash(prev, body):
            return Verification(False, len(records), i, "hash does not match the record's contents")
        prev = rec["hash"]
    if expected_head is not None and prev != expected_head:
        return Verification(False, len(records), len(records), "latest hash does not match the stored head")
    return Verification(True, len(records))


def append(path: str | Path, entry: dict, now: datetime | None = None) -> dict:
    """Add one record to the end of the ledger and return it.

    Refuses to write to a ledger that no longer verifies, so a broken chain is
    noticed instead of being extended.
    """
    path = Path(path)
    check = verify(path)
    if not check.ok:
        raise LedgerError(check.broken_at or 0, check.message)
    records = read(path)
    prev = records[-1]["hash"] if records else GENESIS
    when = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    body = {
        "seq": len(records) + 1,
        "timestamp": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        **{k: v for k, v in entry.items() if k not in ("seq", "timestamp", "prev_hash", "hash")},
        "prev_hash": prev,
    }
    record = {**body, "hash": record_hash(prev, body)}
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_newline = path.exists() and path.stat().st_size > 0 and not path.read_bytes().endswith(b"\n")
    with open(path, "a", encoding="utf-8") as f:
        f.write(("\n" if needs_newline else "") + canonical_json(record) + "\n")
    return record
