"""Single exact margin engine; no calendar guesses, I/O or source parsing."""
from datetime import date
from decimal import Context, Decimal, DecimalException, Inexact, ROUND_HALF_EVEN, localcontext
from uuid import UUID

from .models import (
    BIGINT_MAX, BIGINT_MIN, AdjustmentTreatment, Availability, ConversionEvidence,
    LedgerKind, MarginDailyReport, MarginError, Quantity, QuotaBasis,
)
from .results import (
    DECIMAL_PRECISION, CalculationInput, CalculationResult, CalculationStatus,
)


def _reference(report: MarginDailyReport) -> tuple[CalculationInput, ...]:
    if not isinstance(report, MarginDailyReport):
        raise MarginError('invalid_report')
    return (CalculationInput(
        instrument_id=report.instrument_id, market_id=report.market_id,
        trade_date=report.trade_date, timing=report.timing, lineage=report.lineage,
        financing_policy=report.financing.formula_policy,
        short_margin_policy=report.short_margin.formula_policy,
    ),)


def _exact(operation):
    # The caller's global Decimal precision/traps must never change a result.
    context = Context(prec=DECIMAL_PRECISION, rounding=ROUND_HALF_EVEN)
    context.traps[Inexact] = True
    try:
        with localcontext(context):
            return operation()
    except DecimalException as exc:
        raise MarginError('numeric_precision_exceeded') from exc


def _unavailable(fields: dict[str, Quantity]):
    missing = tuple(name for name, q in fields.items() if q.availability is Availability.MISSING)
    inapplicable = tuple(name for name, q in fields.items()
                         if q.availability is Availability.NOT_APPLICABLE)
    if missing:
        return CalculationStatus.MISSING_DATA, missing + inapplicable, ('missing_fields',)
    if inapplicable:
        return CalculationStatus.NOT_APPLICABLE, inapplicable, ('required_field_not_applicable',)
    return None


class MarginCalculator:
    def financing_identity(self, report: MarginDailyReport) -> CalculationResult:
        refs = _reference(report)
        leg = report.financing
        return self._identity(
            refs, leg, 'financing_balance_identity',
            {'previous_balance': leg.previous_balance, 'purchase': leg.purchase,
             'sale': leg.sale, 'cash_repayment': leg.cash_repayment, 'balance': leg.balance},
            'previous_balance + purchase - sale - cash_repayment',
            (('previous_balance', 1), ('purchase', 1), ('sale', -1), ('cash_repayment', -1)),
        )

    def short_identity(self, report: MarginDailyReport) -> CalculationResult:
        refs = _reference(report)
        leg = report.short_margin
        return self._identity(
            refs, leg, 'short_margin_balance_identity',
            {'previous_balance': leg.previous_balance, 'short_sell': leg.short_sell,
             'short_cover': leg.short_cover, 'stock_repayment': leg.stock_repayment,
             'balance': leg.balance},
            'previous_balance + short_sell - short_cover - stock_repayment',
            (('previous_balance', 1), ('short_sell', 1), ('short_cover', -1), ('stock_repayment', -1)),
        )

    @staticmethod
    def _identity(refs, leg, metric, fields, formula, terms):
        policy = leg.formula_policy
        if policy.adjustment_treatment is AdjustmentTreatment.SIGNED:
            fields = {**fields, 'signed_adjustment': leg.signed_adjustment}
            formula += ' + signed_adjustment'
            terms += (('signed_adjustment', 1),)
        result = dict(metric=metric, quantity_unit=leg.quantity_unit, formula=formula, input_refs=refs)
        if not policy.verified or policy.adjustment_treatment is AdjustmentTreatment.UNVERIFIED:
            return CalculationResult(**result, status=CalculationStatus.NOT_VERIFIED, value=None,
                                     reasons=('balance_formula_not_verified',))
        # NONE excludes an explicitly inapplicable field only. Missing is unknown,
        # and a reported number cannot silently enter an unadjusted source formula.
        if policy.adjustment_treatment is AdjustmentTreatment.NONE:
            if leg.signed_adjustment.availability is Availability.MISSING:
                return CalculationResult(**result, status=CalculationStatus.MISSING_DATA, value=None,
                    missing_fields=('signed_adjustment',), reasons=('adjustment_applicability_missing',))
            if leg.signed_adjustment.availability is not Availability.NOT_APPLICABLE:
                return CalculationResult(**result, status=CalculationStatus.INCOMPATIBLE_DATA, value=None,
                    reasons=('adjustment_not_in_source_formula',))
        unavailable = _unavailable(fields)
        if unavailable:
            status, missing_fields, reasons = unavailable
            return CalculationResult(**result, status=status, value=None,
                                     missing_fields=missing_fields, reasons=reasons)
        expected = _exact(lambda: sum(
            (Decimal(fields[name].value) * sign for name, sign in terms), Decimal(0)))
        reported = Decimal(leg.balance.value)
        difference = _exact(lambda: reported - expected)
        valid = difference == 0
        return CalculationResult(**result,
            status=CalculationStatus.COMPLETE if valid else CalculationStatus.INVALID,
            value=expected if valid else None, expected=expected, reported=reported,
            difference=difference, reasons=() if valid else ('balance_identity_mismatch',))

    def available_quota(self, report: MarginDailyReport, ledger: LedgerKind) -> CalculationResult:
        refs = _reference(report)
        if not isinstance(ledger, LedgerKind):
            raise MarginError('invalid_ledger')
        leg = report.financing if ledger is LedgerKind.FINANCING else report.short_margin
        result = dict(metric=f'{ledger.value}_available_quota', quantity_unit=leg.quantity_unit,
                      formula='verified_same_day_balance_ceiling - reported_balance', input_refs=refs)
        quota = next((q for q in report.quotas if q.ledger is ledger), None)
        if quota is None:
            return CalculationResult(**result, status=CalculationStatus.MISSING_DATA, value=None,
                                     missing_fields=('quota_limit',), reasons=('quota_limit_missing',))
        unavailable = _unavailable({'quota_limit': quota.quantity, 'balance': leg.balance})
        if unavailable:
            status, missing_fields, reasons = unavailable
            return CalculationResult(**result, status=status, value=None,
                                     missing_fields=missing_fields, reasons=reasons)
        if quota.quantity.unit is not leg.quantity_unit:
            return CalculationResult(**result, status=CalculationStatus.INCOMPATIBLE_DATA, value=None,
                                     reasons=('incompatible_quota_unit',))
        reasons = []
        if quota.basis is not QuotaBasis.BALANCE_CEILING:
            reasons.append('quota_basis_not_verified')
        if quota.evidence_version is None:
            reasons.append('quota_evidence_missing')
        if quota.effective_on != report.trade_date:
            reasons.append('quota_effectivity_not_same_day')
        if reasons:
            return CalculationResult(**result, status=CalculationStatus.NOT_VERIFIED,
                                     value=None, reasons=tuple(reasons))
        headroom = _exact(lambda: Decimal(quota.quantity.value) - Decimal(leg.balance.value))
        # Excess is evidence for review; never reject the source report or clamp
        # headroom to zero and call it an executable available quota.
        if headroom < 0:
            return CalculationResult(**result, status=CalculationStatus.INCOMPATIBLE_DATA,
                value=None, expected=headroom, reasons=('negative_headroom_requires_source_policy',))
        return CalculationResult(**result, status=CalculationStatus.COMPLETE,
                                 value=headroom, expected=headroom)

    def convert_quantity(
        self, quantity: Quantity, evidence: ConversionEvidence, *,
        instrument_id: UUID, market_id: UUID, trade_date: date,
    ) -> CalculationResult:
        if not isinstance(quantity, Quantity) or not isinstance(evidence, ConversionEvidence):
            raise MarginError('invalid_conversion_input')
        if not isinstance(instrument_id, UUID) or not isinstance(market_id, UUID):
            raise MarginError('invalid_conversion_identity')
        if type(trade_date) is not date:
            raise MarginError('invalid_conversion_date')
        result = dict(metric='quantity_conversion', quantity_unit=evidence.target_unit,
            formula='raw_quantity * verified_instrument_period_multiplier',
            input_quantity=quantity, conversion_evidence=evidence)
        if (instrument_id != evidence.instrument_id or market_id != evidence.market_id
                or quantity.unit is not evidence.source_unit
                or not evidence.valid_from <= trade_date <= evidence.valid_through):
            return CalculationResult(**result, status=CalculationStatus.INCOMPATIBLE_DATA, value=None,
                                     reasons=('conversion_evidence_not_applicable',))
        unavailable = _unavailable({'quantity': quantity})
        if unavailable:
            status, missing_fields, reasons = unavailable
            return CalculationResult(**result, status=status, value=None,
                                     missing_fields=missing_fields, reasons=reasons)
        converted = _exact(lambda: Decimal(quantity.value) * evidence.factor)
        if converted != converted.to_integral_value():
            return CalculationResult(**result, status=CalculationStatus.INCOMPATIBLE_DATA, value=None,
                                     reasons=('fractional_canonical_quantity',))
        if not BIGINT_MIN <= converted <= BIGINT_MAX:
            return CalculationResult(**result, status=CalculationStatus.INCOMPATIBLE_DATA, value=None,
                                     reasons=('canonical_quantity_out_of_range',))
        return CalculationResult(**result, status=CalculationStatus.COMPLETE, value=converted)
