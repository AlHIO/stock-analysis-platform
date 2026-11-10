"""Lending arithmetic only; no I/O, inferred channels, dates or quota trades."""
from decimal import Context, Decimal, DecimalException, Inexact, ROUND_HALF_EVEN, localcontext

from .models import (
    AdjustmentTreatment, Availability, ChannelPartition, LendingChannel,
    LendingDailyReport, LendingError, ReportKind,
)
from .results import (
    DECIMAL_PRECISION, CalculationInput, CalculationResult, CalculationStatus,
)


def _exact(operation):
    context = Context(prec=DECIMAL_PRECISION, rounding=ROUND_HALF_EVEN)
    context.traps[Inexact] = True
    try:
        with localcontext(context):
            return operation()
    except DecimalException as exc:
        raise LendingError('numeric_precision_exceeded') from exc


def _reference(report):
    if not isinstance(report, LendingDailyReport):
        raise LendingError('invalid_report')
    return CalculationInput(instrument_id=report.instrument_id, market_id=report.market_id,
        trade_date=report.trade_date, report_kind=report.report_kind,
        quantity_unit=report.quantity_unit, channel=report.channel, timing=report.timing,
        lineage=report.lineage, formula_policy=report.ledger.formula_policy)


def _unavailable(fields):
    missing = tuple(name for name, quantity in fields.items()
                    if quantity.availability is Availability.MISSING)
    not_applicable = tuple(name for name, quantity in fields.items()
                           if quantity.availability is Availability.NOT_APPLICABLE)
    if missing:
        return CalculationStatus.MISSING_DATA, missing + not_applicable, ('missing_fields',)
    if not_applicable:
        return CalculationStatus.NOT_APPLICABLE, not_applicable, ('required_field_not_applicable',)
    return None


def _profile(report):
    return (report.instrument_id, report.market_id, report.trade_date, report.report_kind,
            report.quantity_unit, report.lineage.comparison_key,
            report.ledger.formula_policy, report.channel.family, report.channel.version)


class LendingCalculator:
    def borrowing_identity(self, report: LendingDailyReport) -> CalculationResult:
        ref = _reference(report)
        if report.report_kind is not ReportKind.BORROWED_POSITIONS:
            raise LendingError('wrong_report_kind')
        ledger = report.ledger
        return self._identity(report, ref, 'borrowing_balance_identity',
            {'previous_balance':ledger.previous_balance, 'borrowed_quantity':ledger.borrowed_quantity,
             'returned_quantity':ledger.returned_quantity, 'balance':ledger.balance},
            'previous_balance + borrowed_quantity - returned_quantity',
            (('previous_balance',1),('borrowed_quantity',1),('returned_quantity',-1)),
            AdjustmentTreatment.NONE)

    def lending_short_identity(self, report: LendingDailyReport) -> CalculationResult:
        ref = _reference(report)
        if report.report_kind is not ReportKind.LENDING_SHORT_SALES:
            raise LendingError('wrong_report_kind')
        ledger = report.ledger
        return self._identity(report, ref, 'lending_short_balance_identity',
            {'previous_balance':ledger.previous_balance, 'short_sales':ledger.short_sales,
             'short_returns':ledger.short_returns, 'signed_adjustment':ledger.signed_adjustment,
             'balance':ledger.balance},
            'previous_balance + short_sales - short_returns + signed_adjustment',
            (('previous_balance',1),('short_sales',1),('short_returns',-1),('signed_adjustment',1)),
            AdjustmentTreatment.SIGNED)

    @staticmethod
    def _identity(report, ref, metric, fields, formula, terms, treatment):
        policy = report.ledger.formula_policy
        result = dict(metric=metric, quantity_unit=report.quantity_unit, formula=formula,
                      input_refs=(ref,))
        if not policy.verified or policy.adjustment_treatment is not treatment:
            return CalculationResult(**result, value=None, status=CalculationStatus.NOT_VERIFIED,
                                     reasons=('balance_formula_not_verified_for_report_kind',))
        unavailable = _unavailable(fields)
        if unavailable:
            status, missing_fields, reasons = unavailable
            return CalculationResult(**result, value=None, status=status,
                                     missing_fields=missing_fields, reasons=reasons)
        expected = _exact(lambda:sum(
            (Decimal(fields[name].value)*sign for name,sign in terms), Decimal(0)))
        reported = Decimal(report.ledger.balance.value)
        difference = _exact(lambda:reported-expected)
        valid = difference == 0
        return CalculationResult(**result,
            status=CalculationStatus.COMPLETE if valid else CalculationStatus.INVALID,
            value=expected if valid else None, expected=expected, reported=reported,
            difference=difference, reasons=() if valid else ('balance_identity_mismatch',))

    def aggregate_balance(self, reports, *, partition: ChannelPartition) -> CalculationResult:
        if not isinstance(partition, ChannelPartition):
            raise LendingError('invalid_channel_partition')
        if not isinstance(reports, (tuple,list)) or not all(isinstance(r,LendingDailyReport) for r in reports):
            raise LendingError('invalid_channel_reports')
        reports = tuple(reports)
        refs = tuple(_reference(report) for report in reports)
        result = dict(metric='channel_balance_aggregate', quantity_unit=partition.quantity_unit,
            formula='sum complete disjoint member balances under a verified exhaustive channel partition',
            input_refs=refs, partition_version=partition.version,
            partition_evidence_version=partition.evidence_version, partition_evidence=partition)
        def unavailable(status, reason, **extra):
            return CalculationResult(**result, value=None, status=status, reasons=(reason,), **extra)
        if not partition.verified:
            return unavailable(CalculationStatus.NOT_VERIFIED, 'channel_partition_not_verified')
        codes = tuple(report.channel.code for report in reports)
        if len(set(codes)) != len(codes):
            return unavailable(CalculationStatus.INCOMPATIBLE_DATA, 'duplicate_channel_report')
        if partition.total_code in codes or any(
                report.channel.channel is LendingChannel.COMBINED for report in reports):
            return unavailable(CalculationStatus.INCOMPATIBLE_DATA, 'consolidated_total_is_not_a_member')
        if set(codes)-set(partition.member_codes):
            return unavailable(CalculationStatus.INCOMPATIBLE_DATA, 'unexpected_channel_member')
        for report in reports:
            if (report.lineage.source_id != partition.source_id
                    or report.lineage.source_contract_version != partition.source_contract_version
                    or report.lineage.taxonomy_version != partition.taxonomy_version
                    or report.lineage.scope != partition.scope
                    or report.report_kind is not partition.report_kind
                    or report.quantity_unit is not partition.quantity_unit
                    or not partition.valid_from <= report.trade_date <= partition.valid_through
                    or report.channel.family != partition.family
                    or report.channel.version != partition.version):
                return unavailable(CalculationStatus.INCOMPATIBLE_DATA, 'channel_partition_profile_mismatch')
        if reports and any(_profile(report) != _profile(reports[0]) for report in reports):
            return unavailable(CalculationStatus.INCOMPATIBLE_DATA, 'incompatible_channel_reports')
        missing = tuple(code for code in partition.member_codes if code not in codes)
        if missing:
            return unavailable(CalculationStatus.MISSING_DATA, 'missing_channel_members', missing_channels=missing)
        if any(not report.channel.verified or not report.channel.coverage for report in reports):
            return unavailable(CalculationStatus.NOT_VERIFIED, 'channel_coverage_not_verified')
        coverage = set()
        for report in reports:
            if coverage.intersection(report.channel.coverage):
                return unavailable(CalculationStatus.INCOMPATIBLE_DATA, 'overlapping_channel_coverage')
            coverage.update(report.channel.coverage)
        if coverage != set(partition.total_coverage):
            return unavailable(CalculationStatus.INCOMPATIBLE_DATA, 'channel_partition_not_exhaustive')
        first = reports[0]
        publications = {(report.timing.source_published_on,report.timing.source_published_at) for report in reports}
        snapshots = {(report.lineage.raw_id,report.lineage.run_id,report.lineage.payload_sha256)
                     for report in reports}
        # Equal publication timestamps may still describe different corrections.
        # V1 accepts one exact raw snapshot; cross-payload reconciliation needs a
        # future reviewed release-binding contract, not a clock coincidence.
        if len(publications) != 1 or len(snapshots) != 1:
            return unavailable(CalculationStatus.NOT_VERIFIED, 'coherent_source_snapshot_not_verified')
        for report in reports:
            identity = (self.borrowing_identity(report) if report.report_kind is ReportKind.BORROWED_POSITIONS
                        else self.lending_short_identity(report))
            if identity.status is not CalculationStatus.COMPLETE:
                status = (CalculationStatus.INCOMPATIBLE_DATA if identity.status is CalculationStatus.INVALID
                          else identity.status)
                return unavailable(status, 'member_balance_identity_not_complete',
                    missing_fields=tuple(report.channel.code+'.'+field for field in identity.missing_fields))
        value = _exact(lambda:sum((Decimal(report.ledger.balance.value) for report in reports),Decimal(0)))
        return CalculationResult(**result, value=value, expected=value, status=CalculationStatus.COMPLETE)

    def reconcile_channels(self, total: LendingDailyReport, components, *,
                           partition: ChannelPartition) -> CalculationResult:
        ref = _reference(total)
        aggregate = self.aggregate_balance(components, partition=partition)
        result = dict(metric='channel_balance_reconciliation', quantity_unit=aggregate.quantity_unit,
            formula='reported consolidated balance - sum verified disjoint member balances',
            input_refs=(ref, *aggregate.input_refs), partition_version=aggregate.partition_version,
            partition_evidence_version=aggregate.partition_evidence_version,
            partition_evidence=aggregate.partition_evidence)
        if aggregate.status is not CalculationStatus.COMPLETE:
            return CalculationResult(**result, value=None, status=aggregate.status,
                missing_fields=aggregate.missing_fields, missing_channels=aggregate.missing_channels,
                reasons=aggregate.reasons)
        first = components[0]
        if (_profile(total) != _profile(first) or total.channel.code != partition.total_code
                or total.channel.channel is not LendingChannel.COMBINED
                or not total.channel.verified or total.channel.coverage != partition.total_coverage):
            return CalculationResult(**result, value=None, status=CalculationStatus.INCOMPATIBLE_DATA,
                                     reasons=('incompatible_consolidated_channel',))
        same_publication = ((total.timing.source_published_on,total.timing.source_published_at) ==
                            (first.timing.source_published_on,first.timing.source_published_at))
        if not same_publication or (
                total.lineage.raw_id,total.lineage.run_id,total.lineage.payload_sha256) != (
                first.lineage.raw_id,first.lineage.run_id,first.lineage.payload_sha256):
            return CalculationResult(**result, value=None, status=CalculationStatus.NOT_VERIFIED,
                                     reasons=('coherent_source_snapshot_not_verified',))
        identity = (self.borrowing_identity(total) if total.report_kind is ReportKind.BORROWED_POSITIONS
                    else self.lending_short_identity(total))
        if identity.status is not CalculationStatus.COMPLETE:
            return CalculationResult(**result, value=None, status=identity.status,
                                     reasons=('consolidated_balance_identity_not_complete',),
                                     missing_fields=identity.missing_fields)
        reported = Decimal(total.ledger.balance.value)
        difference = _exact(lambda:reported-aggregate.value)
        valid = difference == 0
        return CalculationResult(**result, value=aggregate.value if valid else None,
            expected=aggregate.value, reported=reported, difference=difference,
            status=CalculationStatus.COMPLETE if valid else CalculationStatus.INVALID,
            reasons=() if valid else ('consolidated_balance_mismatch',))
