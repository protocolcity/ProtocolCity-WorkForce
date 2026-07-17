# WorkForce

**Employment infrastructure for agents.**

WorkForce turns AI agents into a staffed workforce. Each worker has identity
papers (a contract and a prompt), a schedule, a budget, and a tamper-evident
shift ledger. One daemon dispatches every shift; one board — the **Roster** —
shows you who is employed, who is on the floor, and what every shift cost.

Part of the [ProtocolCity](https://github.com/protocolcity) suite: pairs
naturally with **WorkLane** (the ticket desk your workers pull work from),
and runs standalone against any queue that can answer a ready-count URL.
Everything is local-first — the roster is a JSON file on your machine, the
ledger is an append-only record, and there is no cloud dependency.

## Quickstart

```
pip install protocolcity-workforce

workforce roster                     # who is employed
workforce dispatch <worker> --dry-run   # rehearse one shift, spend nothing
workforce ledger <worker>            # the shift record
workforce daemon                     # the scheduler — one service, serves the board
workforce open                       # open the Roster board in your browser
```

The board serves at `http://127.0.0.1:8797` by default.

## How it works

- **The roster is data.** Start from [`roster.example.json`](roster.example.json)
  and keep your real roster at `local/roster.json` (gitignored) or point
  `$WORKFORCE_ROSTER` at it. One row per worker: identity, contract path,
  model, cron schedule, budget, queue URL.
- **Workers claim tickets, then work them.** A worker is bound to one
  working directory and pulls from its queue — a WorkLane ready-endpoint
  works out of the box, but any URL that returns a count will do. Empty
  queue means the shift is skipped, not billed.
- **Every shift lands in the ledger.** Outcome, passes, tokens in/out, and
  cost are appended per shift — the record your budgets and reports read.
- **Schedules are data, not services.** The daemon is the only OS service
  you run (`workforce daemon-plist` prints a launchd agent for macOS);
  hiring, pausing, or rescheduling a worker is a roster edit, not a deploy.

## Hiring a worker

```
workforce hire --help
```

Hiring writes the worker's papers (contract + prompt) and adds the roster
row. Contracts are the law a worker reads before every shift — a worker
whose contract says it is not armed will refuse to work, by design.

## Requirements

Python 3.9+. macOS and Linux. Agent CLIs (such as `claude`) are invoked per
shift using the command template in the roster row — bring whichever agent
vendor you employ.

## License

Apache-2.0. Copyright 2026 ProtocolCity.
