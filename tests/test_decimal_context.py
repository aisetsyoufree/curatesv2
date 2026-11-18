"""AUD7-12(f): ambient Decimal precision must never change contract values or verdicts."""

from decimal import Decimal, localcontext

import pytest

from cu_rates.contracts.base import canonical_decimal
from cu_rates.contracts.enums import TierDimension
from cu_rates.validation._rules import _valid_bound


@pytest.mark.parametrize("precision", [1, 3, 6, 28, 50])
def test_canonical_decimal_ignores_ambient_precision(precision: int) -> None:
    with localcontext() as context:
        context.prec = precision
        assert canonical_decimal(Decimal("6.125")) == "6.125"
        assert canonical_decimal(Decimal("6.124")) == "6.124"
        assert canonical_decimal(Decimal("6.125")) != canonical_decimal(Decimal("6.124"))
        assert canonical_decimal(Decimal("1234567.8912345")) == "1234567.8912345"
        assert canonical_decimal(Decimal("100.000")) == "100"
        assert canonical_decimal(Decimal("-0.0")) == "0"


@pytest.mark.parametrize("precision", [1, 3, 28])
def test_money_bound_validation_ignores_ambient_precision(precision: int) -> None:
    with localcontext() as context:
        context.prec = precision
        assert _valid_bound(TierDimension.LOAN_AMOUNT, Decimal("123456.50"))
        assert not _valid_bound(TierDimension.LOAN_AMOUNT, Decimal("123456.505"))
