"""
Variance: what the ledger expected versus what was physically counted.

THE FORMULA (the one the client is buying, stated once, here)
-------------------------------------------------------------

    expected_closing = opening
                     + received
                     - theoretical_consumption      (recipes x units sold)
                     - wastage                      (reason-coded losses)
                     + adjustments                  (authorised corrections)

    variance         = counted_closing - expected_closing
    variance %       = variance / theoretical_consumption
    accuracy         = 100% - |variance %|

WHY THE DENOMINATOR IS CONSUMPTION, NOT STOCK ON HAND
-----------------------------------------------------
Dividing by stock on hand flatters the number - carry a big cheese buffer
and a large loss looks small. Dividing by what you *should have used* is the
honest measure and the harder target, and it is the one this module
implements. A 98% accuracy target means |variance| <= 2% of theoretical
consumption.

Percentages are returned as integer BASIS POINTS (200 = 2.00%) so no float
ever reaches the database or a report total.

Value-weighting: the headline figure for a business is deliberately weighted
by rand value, not a plain average across SKUs. Cheese and chicken dominate
the money; oregano does not, and an unweighted average lets a trivial
oregano miscount swamp a real cheese loss.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from backend.app.inventory.units import value_cents

# A variance at or beyond this share of theoretical consumption is flagged
# for the manager. 200 bp = 2%, the complement of a 98% accuracy target.
DEFAULT_VARIANCE_ALERT_BASIS_POINTS = 200


@dataclass
class IngredientVariance:
    """Computed variance for one ingredient over one stocktake period."""

    opening_milli: int = 0
    received_milli: int = 0
    theoretical_consumption_milli: int = 0
    wastage_milli: int = 0
    adjustment_milli: int = 0
    counted_milli: int = 0
    unit_cost_micro_cents: int = 0

    @property
    def expected_milli(self) -> int:
        return (
            self.opening_milli
            + self.received_milli
            - self.theoretical_consumption_milli
            - self.wastage_milli
            + self.adjustment_milli
        )

    @property
    def variance_milli(self) -> int:
        """Negative means physically short of what the books expected."""
        return self.counted_milli - self.expected_milli

    @property
    def variance_value_cents(self) -> int:
        return value_cents(self.variance_milli, self.unit_cost_micro_cents)

    @property
    def theoretical_value_cents(self) -> int:
        return value_cents(self.theoretical_consumption_milli, self.unit_cost_micro_cents)

    @property
    def variance_basis_points(self) -> int | None:
        """
        Variance as basis points of theoretical consumption.

        None when nothing was consumed in the period - a variance with no
        consumption to divide by has no meaningful percentage, and reporting
        it as 0% or infinity would both be lies.
        """
        return basis_points(self.variance_milli, self.theoretical_consumption_milli)

    @property
    def is_flagged(self) -> bool:
        bp = self.variance_basis_points
        if bp is None:
            # No consumption but stock still moved - always worth a look.
            return self.variance_milli != 0
        return abs(bp) >= DEFAULT_VARIANCE_ALERT_BASIS_POINTS


def basis_points(numerator_milli: int, denominator_milli: int) -> int | None:
    """
    numerator/denominator as integer basis points, rounded half-up.

        basis_points(-1_000, 27_000)  -> -370   (-3.70%)

    Returns None when the denominator is zero.
    """
    if not denominator_milli:
        return None
    exact = (
        Decimal(int(numerator_milli)) * Decimal(10_000) / Decimal(abs(int(denominator_milli)))
    )
    return int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_basis_points(bp: int | None) -> str:
    """-370 -> '-3.70%'; None -> 'n/a'."""
    if bp is None:
        return "n/a"
    return f"{Decimal(bp) / Decimal(100):.2f}%"


def value_weighted_accuracy_basis_points(variances: list[IngredientVariance]) -> int | None:
    """
    The single headline accuracy figure for a business, in basis points.

        10000 = 100.00% accurate;  9800 = 98.00%

    Computed as 10000 minus the absolute rand value of all variance divided
    by the total rand value of theoretical consumption. Weighting by value
    is what stops a trivial miscount of a cheap ingredient dominating the
    number that a manager is judged on.

    Returns None when nothing was consumed anywhere in the period.
    """
    total_theoretical = sum(abs(v.theoretical_value_cents) for v in variances)
    if total_theoretical == 0:
        return None
    total_variance = sum(abs(v.variance_value_cents) for v in variances)
    error_bp = (
        Decimal(total_variance) * Decimal(10_000) / Decimal(total_theoretical)
    ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(Decimal(10_000) - error_bp)
