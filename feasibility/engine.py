"""Candidate implementation goes here.

Implement ``evaluate_offer`` so that it satisfies the rules in ASSIGNMENT.md and
the example expectations in tests/test_cases.py. The dataclasses below define the
required OUTPUT shape (see ASSIGNMENT.md "Output"). You may add helpers, modules,
or rewrite internals freely, but keep ``evaluate_offer``'s signature and the
serialized shape of ``Result`` (so the runner and tests work).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from itertools import combinations

from feasibility.models import (
    Client,
    CreditorRules,
    LedgerEntry,
    Offer,
    default_first_payment_date,
    monthly_payment_dates,
)


@dataclass
class ScheduleRow:
    date: date
    creditor_payment_cents: int
    program_fee_cents: int
    bank_fee_cents: int
    balance_cents: int


@dataclass
class FundsOption:
    amount_cents: int
    within_guardrail: bool
    reason: str
    # lump-sum only:
    date: date | None = None
    # monthly-increment only:
    num_drafts: int | None = None


@dataclass
class AdditionalFunds:
    lump_sum: FundsOption
    monthly_increment: FundsOption


@dataclass
class Result:
    feasible: bool
    # One of "even", "staircase", or "balloon" — the shape your solution produced
    # (driven by the creditor flags). None when infeasible.
    pay_shape_used: str | None = None
    schedule: list[ScheduleRow] | None = None
    additional_funds: AdditionalFunds | None = None

    def to_dict(self) -> dict:
        out: dict = {"feasible": self.feasible, "pay_shape_used": self.pay_shape_used}
        out["schedule"] = (
            [
                {
                    "date": r.date.isoformat(),
                    "creditor_payment_cents": r.creditor_payment_cents,
                    "program_fee_cents": r.program_fee_cents,
                    "bank_fee_cents": r.bank_fee_cents,
                    "balance_cents": r.balance_cents,
                }
                for r in self.schedule
            ]
            if self.schedule is not None
            else None
        )
        if self.additional_funds is None:
            out["additional_funds"] = None
        else:
            def opt(o: FundsOption) -> dict:
                d = {
                    "amount_cents": o.amount_cents,
                    "within_guardrail": o.within_guardrail,
                    "reason": o.reason,
                }
                if o.date is not None:
                    d["date"] = o.date.isoformat()
                if o.num_drafts is not None:
                    d["num_drafts"] = o.num_drafts
                return d

            out["additional_funds"] = {
                "lump_sum": opt(self.additional_funds.lump_sum),
                "monthly_increment": opt(self.additional_funds.monthly_increment),
            }
        return out


@dataclass(frozen=True)
class Candidate:
    shape: str
    payment_dates: list[date]
    payments: list[int]


@dataclass(frozen=True)
class Simulation:
    schedule: list[ScheduleRow]
    fee_cumulative: tuple[int, ...]
    payment_cumulative: tuple[int, ...]


def _round_half_up(value: float | Decimal | str | int) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _pct_cents(pct: float, cents: int) -> int:
    return _round_half_up(Decimal(str(pct)) * Decimal(cents))


def _offer_total(offer: Offer) -> int:
    return _pct_cents(offer.settlement_pct, offer.current_balance_cents)


def _program_fee_total(offer: Offer, rules: CreditorRules) -> int:
    return _pct_cents(rules.program_fee_pct, offer.original_balance_cents)


def _first_payment_date(client: Client, offer: Offer) -> date:
    return offer.first_payment_date or default_first_payment_date(client)


def _cadence_dates_through_horizon(start: date, horizon: date) -> list[date]:
    dates: list[date] = []
    count = 1
    while True:
        d = monthly_payment_dates(start, count)[-1]
        if d > horizon:
            return dates
        dates.append(d)
        count += 1


def _future_entries(client: Client) -> list[LedgerEntry]:
    return [entry for entry in client.ledger if entry.date > client.as_of_date]


def _future_credit_entries(client: Client) -> list[LedgerEntry]:
    return [entry for entry in _future_entries(client) if entry.type == "credit"]


def _lump_sum_date(client: Client) -> date:
    future_dates = [entry.date for entry in _future_entries(client)]
    return min(future_dates) if future_dates else client.as_of_date


def _floor_for_position(position_1based: int, rules: CreditorRules) -> int:
    floor = rules.min_payment_cents
    for from_position, min_cents in rules.min_payment_tiers:
        if position_1based >= from_position:
            floor = max(floor, min_cents)
    if (
        position_1based > rules.max_token_pays
        and floor == rules.min_payment_cents
    ):
        floor = rules.min_payment_cents + 1
    return floor


def _validate_payments(
    payments: list[int],
    offer_total: int,
    rules: CreditorRules,
    shape: str,
) -> bool:
    if not payments:
        return False
    if sum(payments) != offer_total:
        return False
    if any(payment <= 0 for payment in payments):
        return False
    if any(later < earlier for earlier, later in zip(payments, payments[1:])):
        return False
    if any(
        payment < _floor_for_position(i, rules)
        for i, payment in enumerate(payments, start=1)
    ):
        return False
    token_count = sum(1 for payment in payments if payment == rules.min_payment_cents)
    if token_count > rules.max_token_pays:
        return False
    if shape == "even":
        return max(payments) - min(payments) <= 1
    if shape == "staircase":
        return len(set(payments)) <= rules.max_segments
    return True


def _even_payments(total: int, k: int, rules: CreditorRules) -> list[int] | None:
    base, remainder = divmod(total, k)
    payments = [base] * (k - remainder) + [base + 1] * remainder
    return payments if _validate_payments(payments, total, rules, "even") else None


def _balloon_payments(total: int, k: int, rules: CreditorRules) -> list[int] | None:
    if k == 1:
        payments = [total]
    else:
        early = [_floor_for_position(i, rules) for i in range(1, k)]
        final_payment = total - sum(early)
        payments = early + [final_payment]
    return payments if _validate_payments(payments, total, rules, "balloon") else None


def _segmentations(k: int, max_segments: int) -> list[tuple[tuple[int, int], ...]]:
    out: list[tuple[tuple[int, int], ...]] = []
    for segment_count in range(1, min(max_segments, k) + 1):
        for cuts in combinations(range(1, k), segment_count - 1):
            starts = (0,) + cuts
            ends = cuts + (k,)
            out.append(tuple(zip(starts, ends)))
    return out


def _levels_for_segments(
    total: int,
    segment_lengths: tuple[int, ...],
    segment_floors: tuple[int, ...],
) -> tuple[int, ...] | None:
    suffix_min = [0] * (len(segment_lengths) + 1)
    for i in range(len(segment_lengths) - 1, -1, -1):
        suffix_min[i] = suffix_min[i + 1] + segment_lengths[i] * segment_floors[i]

    @lru_cache(maxsize=None)
    def solve(index: int, previous: int, remaining: int) -> tuple[int, ...] | None:
        if index == len(segment_lengths):
            return () if remaining == 0 else None

        length = segment_lengths[index]
        lower = max(previous, segment_floors[index])
        min_after = suffix_min[index + 1]
        high = (remaining - min_after) // length
        for level in range(lower, high + 1):
            next_remaining = remaining - length * level
            suffix = solve(index + 1, level, next_remaining)
            if suffix is not None:
                return (level,) + suffix
        return None

    return solve(0, 0, total)


def _staircase_payments(total: int, k: int, rules: CreditorRules) -> list[int] | None:
    floors = [_floor_for_position(i, rules) for i in range(1, k + 1)]
    best: list[int] | None = None
    for segmentation in _segmentations(k, rules.max_segments):
        lengths = tuple(end - start for start, end in segmentation)
        segment_floors = tuple(max(floors[start:end]) for start, end in segmentation)
        levels = _levels_for_segments(total, lengths, segment_floors)
        if levels is None:
            continue
        payments: list[int] = []
        for level, length in zip(levels, lengths):
            payments.extend([level] * length)
        if not _validate_payments(payments, total, rules, "staircase"):
            continue
        if best is None or tuple(payments) < tuple(best):
            best = payments
    return best


def _generate_candidate(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
    k: int,
) -> Candidate | None:
    start = _first_payment_date(client, offer)
    payment_dates = monthly_payment_dates(start, k)
    if not payment_dates or any(d > client.last_draft_date for d in payment_dates):
        return None

    total = _offer_total(offer)
    if rules.even_pays:
        shape = "even"
        payments = _even_payments(total, k, rules)
    elif rules.is_ballooning_allowed:
        shape = "balloon"
        payments = _balloon_payments(total, k, rules)
    else:
        shape = "staircase"
        payments = _staircase_payments(total, k, rules)

    if payments is None:
        return None
    return Candidate(shape=shape, payment_dates=payment_dates, payments=payments)


def _simulate(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
    candidate: Candidate,
    extra_credit: tuple[date, int] | None = None,
    monthly_increment: int = 0,
) -> Simulation | None:
    horizon = client.last_draft_date
    first_payment = _first_payment_date(client, offer)
    cadence_dates = _cadence_dates_through_horizon(first_payment, horizon)
    cadence_set = set(cadence_dates)
    payment_by_date = dict(zip(candidate.payment_dates, candidate.payments))
    remaining_fee = _program_fee_total(offer, rules)
    balance = client.current_balance_cents
    rows: list[ScheduleRow] = []
    fee_cumulative: list[int] = []
    payment_cumulative: list[int] = []
    total_fee_collected = 0
    total_creditor_paid = 0

    dates = {
        entry.date
        for entry in _future_entries(client)
        if entry.date <= horizon
    } | cadence_set
    if extra_credit is not None:
        dates.add(extra_credit[0])

    for current in sorted(dates):
        if current > horizon:
            return None

        for entry in client.ledger:
            if entry.date != current or entry.date <= client.as_of_date:
                continue
            if entry.type == "credit":
                balance += entry.amount_cents
                if monthly_increment:
                    balance += monthly_increment

        if extra_credit is not None and current == extra_credit[0]:
            balance += extra_credit[1]

        for entry in client.ledger:
            if entry.date != current or entry.date <= client.as_of_date:
                continue
            if entry.type == "debit":
                balance -= entry.amount_cents

        creditor_payment = payment_by_date.get(current, 0)
        bank_fee = rules.bank_fee_cents if creditor_payment > 0 else 0
        balance -= creditor_payment + bank_fee
        if balance < 0:
            return None

        program_fee = 0
        if current in cadence_set and current >= first_payment and remaining_fee > 0:
            program_fee = min(balance, remaining_fee)
            balance -= program_fee
            remaining_fee -= program_fee
            total_fee_collected += program_fee

        if balance < 0:
            return None

        if current in cadence_set:
            total_creditor_paid += creditor_payment
            fee_cumulative.append(total_fee_collected)
            payment_cumulative.append(total_creditor_paid)

        if creditor_payment > 0 or program_fee > 0:
            rows.append(
                ScheduleRow(
                    date=current,
                    creditor_payment_cents=creditor_payment,
                    program_fee_cents=program_fee,
                    bank_fee_cents=bank_fee,
                    balance_cents=balance,
                )
            )

    if remaining_fee != 0:
        return None
    return Simulation(
        schedule=rows,
        fee_cumulative=tuple(fee_cumulative),
        payment_cumulative=tuple(payment_cumulative),
    )


def _candidate_sort_key(candidate: Candidate, simulation: Simulation) -> tuple:
    return (
        simulation.fee_cumulative,
        tuple(-value for value in simulation.payment_cumulative),
        len(candidate.payments),
    )


def _solve_feasible(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
    extra_credit: tuple[date, int] | None = None,
    monthly_increment: int = 0,
) -> Result | None:
    max_k = min(rules.max_terms, rules.max_payments)
    best: tuple[tuple, Candidate, Simulation] | None = None
    for k in range(1, max_k + 1):
        candidate = _generate_candidate(client, offer, rules, k)
        if candidate is None:
            continue
        simulation = _simulate(
            client,
            offer,
            rules,
            candidate,
            extra_credit=extra_credit,
            monthly_increment=monthly_increment,
        )
        if simulation is None:
            continue
        key = _candidate_sort_key(candidate, simulation)
        if best is None or key > best[0]:
            best = (key, candidate, simulation)

    if best is None:
        return None
    _, candidate, simulation = best
    return Result(
        feasible=True,
        pay_shape_used=candidate.shape,
        schedule=simulation.schedule,
        additional_funds=None,
    )


def _find_min_amount(predicate) -> int | None:
    if predicate(0):
        return 0
    high = 1
    while not predicate(high):
        high *= 2
        if high > 10_000_000_000:
            return None
    low = 1
    while low < high:
        mid = (low + high) // 2
        if predicate(mid):
            high = mid
        else:
            low = mid + 1
    return low


def _min_lump_sum(client: Client, offer: Offer, rules: CreditorRules) -> FundsOption:
    extra_date = _lump_sum_date(client)

    def works(amount: int) -> bool:
        return _solve_feasible(
            client,
            offer,
            rules,
            extra_credit=(extra_date, amount),
        ) is not None

    amount = _find_min_amount(works)
    if amount is None:
        return FundsOption(
            amount_cents=0,
            within_guardrail=False,
            reason="no lump sum can satisfy structural payment constraints",
            date=extra_date,
        )
    guardrail = _round_half_up(Decimal("0.65") * Decimal(_offer_total(offer)))
    within = amount <= guardrail
    reason = "" if within else f"exceeds lump-sum guardrail of {guardrail} cents"
    return FundsOption(
        amount_cents=amount,
        within_guardrail=within,
        reason=reason,
        date=extra_date,
    )


def _min_monthly_increment(
    client: Client,
    offer: Offer,
    rules: CreditorRules,
) -> FundsOption:
    future_credits = _future_credit_entries(client)
    count = len(future_credits)
    if count == 0:
        return FundsOption(
            amount_cents=0,
            within_guardrail=False,
            reason="no future drafts to increase",
            num_drafts=0,
        )

    def works(amount: int) -> bool:
        return _solve_feasible(
            client,
            offer,
            rules,
            monthly_increment=amount,
        ) is not None

    amount = _find_min_amount(works)
    if amount is None:
        return FundsOption(
            amount_cents=0,
            within_guardrail=False,
            reason="no monthly increment can satisfy structural payment constraints",
            num_drafts=count,
        )
    guardrail = max(
        10000,
        _round_half_up(Decimal("0.40") * Decimal(client.draft_amount_cents)),
    )
    within = amount <= guardrail
    reason = "" if within else f"exceeds monthly-increment guardrail of {guardrail} cents"
    return FundsOption(
        amount_cents=amount,
        within_guardrail=within,
        reason=reason,
        num_drafts=count,
    )


def evaluate_offer(client: Client, offer: Offer, rules: CreditorRules) -> Result:
    """Evaluate a single offer. See ASSIGNMENT.md for the full specification.

    Return a Result with feasible=True and a schedule when the offer fits, or
    feasible=False with additional_funds (minimum lump sum AND minimum monthly
    increment) when it does not.
    """
    feasible = _solve_feasible(client, offer, rules)
    if feasible is not None:
        return feasible

    return Result(
        feasible=False,
        pay_shape_used=None,
        schedule=None,
        additional_funds=AdditionalFunds(
            lump_sum=_min_lump_sum(client, offer, rules),
            monthly_increment=_min_monthly_increment(client, offer, rules),
        ),
    )
