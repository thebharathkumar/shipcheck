# 90-second demo script

Setup before recording: run `shipcheck demo`, open `examples/report.html` in a browser, and have a terminal open at the repo root. Everything shown is simulated data.

## 0:00 to 0:10, the question

Show the top of the report.

> "Before an agent change goes live, a clinic needs to know one thing: is it safe to ship?"

## 0:10 to 0:40, v2 is blocked

The report opens on **v2 vs v1**. Point at the two cards, then the change strip under them.

> "v2 swaps in a smaller model and a new prompt. At example rates it's 70% cheaper per 1,000 runs, and it scores slightly better on average: 88% of cases correct, up from 85%. shipcheck blocks it anyway."

Under **Got worse**, open **Case R-017**. The highlighted step is the send.

> "It sent a referral before staff approved it. Approval came one step later."

## 0:40 to 1:05, v3 needs a person

Click **v3 vs v1** at the top.

> "v3 adds a guardrail in code. The savings stay and the safety issues are gone. One case changed specialist, to one outside the patient's network, so it needs a human: REVIEW."

Open **Case R-031** under Got worse to show the specialist and plan.

## 1:05 to 1:20, approve and verify

Click **Approve v3** to show the command the page cannot run itself, then run it in the terminal:

```
shipcheck approve --candidate v3 --by "Clinic Ops Lead" --note "Reviewed R-031"
shipcheck verify-ledger
```

> "The approval is recorded with who, why and the results it was based on. The log is hash-chained, so an edited record shows up as broken."

## 1:20 to 1:30, close

> "No API key, no network, all synthetic data. The checks are code, not a model's opinion."

Notes for the presenter:

- Say "at example rates" when quoting cost. The rate card is invented and labelled EXAMPLE RATES, NOT REAL PRICES.
- If asked about the pass rates: with 40 cases the ranges are wide, and the gate does not rely on them. It relies on which cases got worse.
- To reset between takes, delete `approvals.jsonl` at the repo root (it is ignored by git) and reopen `examples/report.html` without `#v3-vs-v1` at the end of the address, so it opens on v2 vs v1 again.
