# Settlement Feasibility & Fee Engine — Take-home

Welcome, and thanks for taking the time. The full problem is in
[`ASSIGNMENT.md`](./ASSIGNMENT.md). This README is just orientation.

## The task in one line

Given a client's escrow account, a settlement offer, and a creditor's rules,
decide whether the offer is affordable (and schedule it, collecting our fee as
early as allowed) or — if not — compute the minimum extra funding needed.

## Setup

```bash
uv sync
```

## Layout

```
hiring_takehome/
├── ASSIGNMENT.md            # full specification — read this
├── feasibility/
│   ├── models.py            # data models, JSON loaders, date/EOM helpers (provided)
│   └── engine.py            # >>> implement evaluate_offer here <<< (+ Result shape)
├── cases/                   # four example cases (client.json / offer.json / creditor_rules.json)
│   ├── case1_feasible_even
│   ├── case2_infeasible_minima
│   ├── case3_balloon
│   └── case4_tiers
├── tests/
│   ├── test_smoke.py        # scaffolding sanity tests
│   ├── test_cases.py        # provided case expectations
│   └── test_engine_edge_cases.py # additional edge-case coverage
├── run.py                   # uv run python run.py cases/<case>
└── requirements.txt
```

## Run

```bash
# evaluate a single case (prints the Result as JSON)
uv run python run.py cases/case1_feasible_even

# tests
uv run pytest -q
```

The test suite includes the provided case expectations plus additional edge-case
coverage for rounding, payment shapes, ledger ordering, fee timing, horizon
limits, and additional-funds minima.

## What to submit

Your implementation, your tests, and a short README section describing:
- your approach and the alternatives you considered,
- **your interpretation of the payment shapes** (even / staircase / balloon — we
  left these loosely defined on purpose),
- assumptions you made, and known edge cases / limitations.

Budget ~5–6 hours. Prefer a correct, well-tested core over breadth. When in
doubt, write down your assumption and keep going.

## Implementation Notes

### Approach

I split the implementation into two parts: generating valid creditor-payment candidates and simulating the escrow ledger chronologically. For each allowed payment count, I build the creditor-payment shape implied by the creditor flags, validate the hard constraints, then simulate future ledger entries, creditor payments, bank fees, and program fees date by date.

I modeled the objective as a lexicographic comparison of cumulative program fee collected on cadence dates: a schedule that has collected more fee earlier wins.

If two schedules collect fees equally early, I use less cumulative creditor payment earlier as the next tie-breaker, then the larger payment count.

I considered trying to derive a closed-form schedule directly from the account cash flow. I chose candidate search instead because the problem has small natural bounds (`max_terms` / `max_payments`) and the rules are easier to test when shape generation and ledger simulation are separate.

### Payment Shape Interpretation

- **Even**: payments are as equal as possible for the chosen count. Remainder cents are put on the latest payments so the sequence remains non-decreasing.
- **Balloon**: I set all non-final payments to their minimum legal floors, and the final payment absorbs the remaining settlement balance. Token limits and tier floors still apply to every payment, including the final balloon.
- **Staircase**: a staircase is a set of contiguous equal-payment segments. The solver searches segment placements up to `max_segments`. I choose the lexicographically smallest valid payment sequence, which keeps early creditor payments low and frees cash for earlier fee collection.

### Assumptions

- In this scaffold, `Offer.current_balance_cents` is the creditor balance. The assignment text mentions a later `creditor_balance_cents` rename, but the provided models and JSON files still use `current_balance_cents`.
- All monetary percentage calculations use explicit round-half-up rounding. 
- Future ledger entries dated on or before `as_of_date` are ignored because they are already reflected in `client.current_balance_cents`.
- Same-day ordering is credits first, then fixed debits, then creditor payment and bank fee, then discretionary program fee.
- Program fee is collected greedily on cadence dates after required same-day debits. Fee-only cadence dates are allowed and do not incur a bank fee.
- The monthly-increment calculation treats all future ledger credits as drafts, matching the assignment statement that the ledger credits are the drafts.
- A lump-sum funding option is placed on the earliest future ledger date after `as_of_date`, or on `as_of_date` if there are no future ledger entries.

### Known Limitations

- The staircase generator searches segment placements rather than solving a general integer program. This is appropriate for the small take-home bounds but would need a more specialized optimizer for very large production limits.
- Structural impossibility in the additional-funds search is reported after a large bounded search rather than with a full proof certificate.
- I preserved the provided public dataclasses and JSON shape rather than renaming the offer balance field to the assignment's newer terminology.
