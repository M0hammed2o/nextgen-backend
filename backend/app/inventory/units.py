"""
Unit conversion for stock quantities.

THE STORAGE RULE
----------------
Every stock quantity persisted anywhere in this platform is an ``int``
counting **thousandths of the ingredient's canonical unit**:

    canonical units:  GRAM (mass) | MILLILITRE (volume) | UNIT (countable)
    stored value:     quantity_milli = canonical_quantity * 1000

So 150 g is stored as ``150_000``; one dough ball as ``1_000``; 2.5 kg as
``2_500_000``.

WHY
---
This is the same decision the platform already made for money (cents), for
the same reason: binary floating point cannot represent 0.1 exactly, and a
stock ledger that accumulates thousands of small deductions in float
arithmetic will drift. Drift in a variance report is indistinguishable
from theft, which makes it worse than useless. Integers cannot drift.

Kilograms, litres, cases and packs are **purchase** units. They convert to
canonical milli-units at the edge - on import, on goods receipt, on recipe
entry - and are never stored.

Resolution is 0.001 of a canonical unit (a milligram), roughly three orders
of magnitude finer than any kitchen scale, so quantisation here is not a
practical source of error.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from shared.enums import CanonicalUnit, MeasureFamily

# Thousandths of a canonical unit per canonical unit.
MILLI: int = 1000


# -- Which canonical unit each family stores in -------------------------------

FAMILY_CANONICAL: dict[MeasureFamily, CanonicalUnit] = {
    MeasureFamily.MASS: CanonicalUnit.GRAM,
    MeasureFamily.VOLUME: CanonicalUnit.MILLILITRE,
    MeasureFamily.COUNT: CanonicalUnit.UNIT,
}

CANONICAL_FAMILY: dict[CanonicalUnit, MeasureFamily] = {
    v: k for k, v in FAMILY_CANONICAL.items()
}


# -- Purchase / entry units, and their size in canonical units ----------------
#
# Keyed by the uppercase symbol a user types or a spreadsheet column holds.
# The Decimal is how many canonical units one of this unit represents.

UNIT_DEFS: dict[str, tuple[MeasureFamily, Decimal]] = {
    # mass -> gram
    "MG": (MeasureFamily.MASS, Decimal("0.001")),
    "G": (MeasureFamily.MASS, Decimal("1")),
    "GRAM": (MeasureFamily.MASS, Decimal("1")),
    "GRAMS": (MeasureFamily.MASS, Decimal("1")),
    "KG": (MeasureFamily.MASS, Decimal("1000")),
    "KILOGRAM": (MeasureFamily.MASS, Decimal("1000")),
    # volume -> millilitre
    "ML": (MeasureFamily.VOLUME, Decimal("1")),
    "MILLILITRE": (MeasureFamily.VOLUME, Decimal("1")),
    "L": (MeasureFamily.VOLUME, Decimal("1000")),
    "LITRE": (MeasureFamily.VOLUME, Decimal("1000")),
    "LITER": (MeasureFamily.VOLUME, Decimal("1000")),
    # count -> unit
    "UNIT": (MeasureFamily.COUNT, Decimal("1")),
    "UNITS": (MeasureFamily.COUNT, Decimal("1")),
    "EACH": (MeasureFamily.COUNT, Decimal("1")),
    "EA": (MeasureFamily.COUNT, Decimal("1")),
    "PORTION": (MeasureFamily.COUNT, Decimal("1")),
}

# Units we are willing to *display* in, largest first, per family. Renders
# 10_000_000 milli-grams as "10 kg" rather than "10000 g".
DISPLAY_LADDER: dict[MeasureFamily, list[tuple[str, Decimal]]] = {
    MeasureFamily.MASS: [("kg", Decimal("1000")), ("g", Decimal("1"))],
    MeasureFamily.VOLUME: [("L", Decimal("1000")), ("ml", Decimal("1"))],
    MeasureFamily.COUNT: [("", Decimal("1"))],
}


class UnitError(ValueError):
    """An unknown unit, or a conversion between incompatible families."""


def normalise_unit(unit: str) -> str:
    """'kg ' -> 'KG'. Raises UnitError for anything not in UNIT_DEFS."""
    if unit is None:
        raise UnitError("A unit is required")
    key = str(unit).strip().upper()
    if key not in UNIT_DEFS:
        raise UnitError(
            f"Unknown unit {unit!r}. Known units: "
            f"{', '.join(sorted(set(UNIT_DEFS)))}"
        )
    return key


def family_of(unit: str) -> MeasureFamily:
    """The measure family a unit symbol belongs to."""
    return UNIT_DEFS[normalise_unit(unit)][0]


def canonical_unit_for(unit: str) -> CanonicalUnit:
    """The canonical unit that quantities in `unit` are stored as."""
    return FAMILY_CANONICAL[family_of(unit)]


def _as_decimal(quantity) -> Decimal:
    """
    Accept int / str / Decimal. Floats are accepted but routed through str()
    so 0.1 becomes Decimal('0.1') rather than the binary approximation -
    callers should still prefer str or Decimal.
    """
    if isinstance(quantity, Decimal):
        return quantity
    if isinstance(quantity, int) and not isinstance(quantity, bool):
        return Decimal(quantity)
    try:
        return Decimal(str(quantity))
    except (InvalidOperation, TypeError) as exc:
        raise UnitError(f"{quantity!r} is not a valid quantity") from exc


def to_milli(quantity, unit: str, *, canonical: CanonicalUnit | None = None) -> int:
    """
    Convert a human quantity in `unit` to canonical milli-units.

        to_milli(2, "kg")   -> 2_000_000     (2 kg = 2000 g)
        to_milli(150, "g")  -> 150_000
        to_milli(1, "unit") -> 1_000

    If `canonical` is given the unit must belong to that canonical unit's
    family - the guard that stops 2 litres of oil being received against an
    ingredient measured in grams.
    """
    key = normalise_unit(unit)
    family, factor = UNIT_DEFS[key]

    if canonical is not None and CANONICAL_FAMILY[canonical] is not family:
        raise UnitError(
            f"Cannot express {key} (a {family.value} unit) in "
            f"{canonical.value} - these are different kinds of measurement"
        )

    exact = _as_decimal(quantity) * factor * MILLI
    return int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_milli(quantity_milli: int, unit: str) -> Decimal:
    """
    Convert stored milli-units back into `unit`, exactly.

        from_milli(10_000_000, "kg") -> Decimal('10')
        from_milli(150_000, "g")     -> Decimal('150')
    """
    key = normalise_unit(unit)
    _family, factor = UNIT_DEFS[key]
    return (Decimal(int(quantity_milli)) / Decimal(MILLI)) / factor


def to_canonical(quantity_milli: int) -> Decimal:
    """Stored milli-units as a plain canonical quantity (150_000 -> 150)."""
    return Decimal(int(quantity_milli)) / Decimal(MILLI)


def pack_to_milli(pack_count, pack_size_milli: int) -> int:
    """
    Receiving whole purchase packs.

        5 bags of 2 kg  ->  pack_to_milli(5, to_milli(2, "kg"))
                        ->  10_000_000 milli-grams == 10 000 g

    `pack_count` may be fractional (half a bag), hence Decimal.
    """
    exact = _as_decimal(pack_count) * Decimal(int(pack_size_milli))
    return int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _trim(d: Decimal) -> str:
    """Decimal('10.000') -> '10'; Decimal('0.500') -> '0.5'."""
    normalised = d.normalize()
    # normalize() renders small integers in exponent form (1E+1); expand it.
    if normalised == normalised.to_integral_value():
        normalised = normalised.quantize(Decimal("1"))
    return format(normalised, "f")


def format_quantity(quantity_milli: int, canonical: CanonicalUnit) -> str:
    """
    Human-readable, picking the largest sensible unit.

        format_quantity(10_000_000, GRAM) -> '10 kg'
        format_quantity(150_000, GRAM)    -> '150 g'
        format_quantity(72_000, UNIT)     -> '72'
    """
    family = CANONICAL_FAMILY[canonical]
    canonical_qty = to_canonical(quantity_milli)
    negative = canonical_qty < 0
    magnitude = -canonical_qty if negative else canonical_qty

    for symbol, size in DISPLAY_LADDER[family]:
        if magnitude >= size or size == Decimal("1"):
            text = _trim(magnitude / size)
            out = f"{text} {symbol}".strip()
            return f"-{out}" if negative else out

    return _trim(canonical_qty)


def value_cents(quantity_milli: int, cost_per_canonical_micro_cents: int) -> int:
    """
    Rand value of a stock quantity.

    Cost is held as **micro-cents per canonical unit** (cents x 1 000 000) so
    cheap-per-gram ingredients keep precision: a R38/kg tomato base is
    0.38 cents per gram, which would truncate to zero in whole cents.

        cheese at R95/kg == 9.5 cents/g == 9_500_000 micro-cents/g
        value_cents(150_000, 9_500_000) -> 1425   (150 g = R14.25)

    Rounded half-up to whole cents once, at the very last step.
    """
    exact = (
        Decimal(int(quantity_milli))
        * Decimal(int(cost_per_canonical_micro_cents))
        / (Decimal(MILLI) * Decimal(1_000_000))
    )
    return int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def cost_per_canonical_micro_cents(pack_cost_cents: int, pack_size_milli: int) -> int:
    """
    Derive per-canonical-unit cost from what a purchase pack costs.

        A 2 kg cheese bag costing R190 (19000c):
            cost_per_canonical_micro_cents(19000, 2_000_000)
            -> 9_500_000  == 9.5 cents per gram == R95/kg
    """
    if pack_size_milli <= 0:
        raise UnitError("Pack size must be greater than zero")
    exact = (
        Decimal(int(pack_cost_cents))
        * Decimal(1_000_000)
        * Decimal(MILLI)
        / Decimal(int(pack_size_milli))
    )
    return int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
