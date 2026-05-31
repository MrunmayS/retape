from __future__ import annotations

from datetime import date

from feasibility.engine import evaluate_offer
from feasibility.models import (
    Client,
    CreditorRules,
    LedgerEntry,
    Offer,
    load_case,
    load_offer,
    offer_total_cents,
    program_fee_cents,
)


def _client(
    ledger: list[LedgerEntry],
    *,
    last_draft_date: date,
    draft_amount_cents: int = 10000,
) -> Client:
    return Client(
        draft_amount_cents=draft_amount_cents,
        draft_day=1,
        first_draft_date=date(2026, 1, 1),
        last_draft_date=last_draft_date,
        as_of_date=date(2025, 12, 31),
        current_balance_cents=0,
        ledger=ledger,
    )


def _offer(
    *,
    creditor_balance_cents: int,
    original_balance_cents: int | None = None,
    settlement_pct: float = 1.0,
    first_payment_date: date = date(2026, 1, 31),
) -> Offer:
    return Offer(
        creditor="Synthetic",
        current_balance_cents=creditor_balance_cents,
        original_balance_cents=(
            creditor_balance_cents
            if original_balance_cents is None
            else original_balance_cents
        ),
        settlement_pct=settlement_pct,
        first_payment_date=first_payment_date,
    )


def _rules(
    *,
    max_terms: int = 6,
    max_payments: int = 6,
    min_payment_cents: int = 1,
    max_token_pays: int = 99,
    min_payment_tiers: list[tuple[int, int]] | None = None,
    even_pays: bool = False,
    is_ballooning_allowed: bool = False,
    max_segments: int = 4,
    bank_fee_cents: int = 0,
    program_fee_pct: float = 0.0,
) -> CreditorRules:
    return CreditorRules(
        max_terms=max_terms,
        max_payments=max_payments,
        min_payment_cents=min_payment_cents,
        max_token_pays=max_token_pays,
        min_payment_tiers=min_payment_tiers or [],
        even_pays=even_pays,
        is_ballooning_allowed=is_ballooning_allowed,
        max_segments=max_segments,
        bank_fee_cents=bank_fee_cents,
        program_fee_pct=program_fee_pct,
    )


def test_money_helpers_use_round_half_up():
    offer = _offer(creditor_balance_cents=101, original_balance_cents=101, settlement_pct=0.5)
    rules = _rules(program_fee_pct=0.5)

    assert offer_total_cents(offer) == 51
    assert program_fee_cents(offer, rules) == 51


def test_offer_loader_accepts_assignment_balance_field(tmp_path):
    offer_path = tmp_path / "offer.json"
    offer_path.write_text(
        """
        {
          "creditor": "RenamedFieldCo",
          "creditor_balance_cents": 101,
          "original_balance_cents": 101,
          "settlement_pct": 0.5,
          "first_payment_date": "2026-01-31"
        }
        """
    )

    offer = load_offer(offer_path)

    assert offer.current_balance_cents == 101


def test_provided_feasible_cases_satisfy_schedule_invariants():
    for case in ["case1_feasible_even", "case3_balloon", "case4_tiers"]:
        client, offer, rules = load_case(f"cases/{case}")
        result = evaluate_offer(client, offer, rules)

        assert result.feasible is True
        assert result.schedule is not None
        assert all(row.balance_cents >= 0 for row in result.schedule)
        assert all(row.date <= client.last_draft_date for row in result.schedule)
        assert (
            sum(row.creditor_payment_cents for row in result.schedule)
            == offer_total_cents(offer)
        )
        assert sum(row.program_fee_cents for row in result.schedule) == program_fee_cents(
            offer, rules
        )
        assert all(
            row.bank_fee_cents == rules.bank_fee_cents
            for row in result.schedule
            if row.creditor_payment_cents > 0
        )
        assert all(
            row.bank_fee_cents == 0
            for row in result.schedule
            if row.creditor_payment_cents == 0
        )


def test_even_remainder_goes_to_latest_payments():
    client = _client(
        [
            LedgerEntry(date(2026, 1, 1), 10000, "credit"),
            LedgerEntry(date(2026, 2, 1), 10000, "credit"),
            LedgerEntry(date(2026, 3, 1), 10000, "credit"),
        ],
        last_draft_date=date(2026, 3, 31),
    )
    result = evaluate_offer(
        client,
        _offer(creditor_balance_cents=10000),
        _rules(max_terms=3, max_payments=3, even_pays=True),
    )

    assert result.feasible is True
    assert [row.creditor_payment_cents for row in result.schedule] == [3333, 3333, 3334]


def test_same_day_credits_are_applied_before_debits_and_zero_balance_is_valid():
    client = _client(
        [LedgerEntry(date(2026, 1, 1), 10000, "credit")],
        last_draft_date=date(2026, 1, 1),
    )
    result = evaluate_offer(
        client,
        _offer(creditor_balance_cents=10000, first_payment_date=date(2026, 1, 1)),
        _rules(max_terms=1, max_payments=1),
    )

    assert result.feasible is True
    assert result.schedule[-1].date == date(2026, 1, 1)
    assert result.schedule[-1].balance_cents == 0


def test_program_fee_is_not_collected_before_first_payment_date():
    client = _client(
        [LedgerEntry(date(2026, 1, 1), 10000, "credit")],
        last_draft_date=date(2026, 1, 31),
    )
    result = evaluate_offer(
        client,
        _offer(creditor_balance_cents=1000, original_balance_cents=9000),
        _rules(max_terms=1, max_payments=1, min_payment_cents=1000, program_fee_pct=1.0),
    )

    assert result.feasible is True
    assert [row.date for row in result.schedule] == [date(2026, 1, 31)]
    assert result.schedule[0].program_fee_cents == 9000


def test_fee_only_month_has_no_bank_fee():
    client = _client(
        [
            LedgerEntry(date(2026, 1, 1), 3000, "credit"),
            LedgerEntry(date(2026, 2, 1), 3000, "credit"),
        ],
        draft_amount_cents=3000,
        last_draft_date=date(2026, 3, 1),
    )
    result = evaluate_offer(
        client,
        _offer(creditor_balance_cents=2500, original_balance_cents=3000),
        _rules(
            max_terms=1,
            max_payments=1,
            min_payment_cents=2500,
            bank_fee_cents=500,
            program_fee_pct=1.0,
        ),
    )

    assert result.feasible is True
    fee_only_rows = [row for row in result.schedule if row.creditor_payment_cents == 0]
    assert fee_only_rows
    assert all(row.bank_fee_cents == 0 for row in fee_only_rows)


def test_horizon_blocks_payment_dates_after_last_draft_date():
    client = _client(
        [LedgerEntry(date(2026, 1, 1), 100000, "credit")],
        last_draft_date=date(2026, 1, 1),
    )
    result = evaluate_offer(
        client,
        _offer(creditor_balance_cents=1000, first_payment_date=date(2026, 1, 31)),
        _rules(max_terms=1, max_payments=1),
    )

    assert result.feasible is False
    assert result.schedule is None


def test_token_pay_limit_pushes_later_minimums_above_base_min():
    client = _client(
        [
            LedgerEntry(date(2026, 1, 1), 10000, "credit"),
            LedgerEntry(date(2026, 2, 1), 10000, "credit"),
            LedgerEntry(date(2026, 3, 1), 10000, "credit"),
        ],
        last_draft_date=date(2026, 3, 31),
    )
    result = evaluate_offer(
        client,
        _offer(creditor_balance_cents=7502),
        _rules(
            max_terms=3,
            max_payments=3,
            min_payment_cents=2500,
            max_token_pays=1,
            is_ballooning_allowed=True,
        ),
    )

    assert result.feasible is True
    payments = [row.creditor_payment_cents for row in result.schedule]
    assert payments == [2500, 2501, 2501]
    assert payments.count(2500) == 1


def test_tier_floors_and_max_segments_on_staircase_case():
    client, offer, rules = load_case("cases/case4_tiers")
    result = evaluate_offer(client, offer, rules)

    assert result.feasible is True
    payments = [row.creditor_payment_cents for row in result.schedule if row.creditor_payment_cents]
    assert all(payment >= 5000 for payment in payments[6:])
    assert len(set(payments)) <= rules.max_segments


def test_case2_minimum_additional_funds_are_exact():
    client, offer, rules = load_case("cases/case2_infeasible_minima")
    result = evaluate_offer(client, offer, rules)

    assert result.feasible is False
    assert result.additional_funds.lump_sum.amount_cents == 10000
    assert result.additional_funds.monthly_increment.amount_cents == 2500
    assert result.additional_funds.monthly_increment.num_drafts == 5
