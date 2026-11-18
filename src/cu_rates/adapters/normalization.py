"""Attach source-stated scenario proofs without inferring unstated assumptions."""

from datetime import datetime, timedelta
from decimal import Decimal

from cu_rates.contracts.enums import RateType, ScenarioDimension
from cu_rates.contracts.offers import NormalizedOffer
from cu_rates.contracts.scenarios import StatedBound, statement_matches_excerpt
from cu_rates.normalize import ScenarioCheck
from cu_rates.normalize._values import Range, comparison_statement


def stated_bound(dimension: str, excerpt: str) -> StatedBound | None:
    """Type the excerpt. A statement the excerpt does not literally support is dropped."""
    try:
        value = comparison_statement(dimension, excerpt)
    except ValueError:
        return None
    if value is None:
        return None
    if isinstance(value, Range):
        bound = StatedBound(lower=value.lower, upper=value.upper)
    elif isinstance(value, Decimal):
        bound = StatedBound(exact=value)
    elif isinstance(value, bool):
        bound = StatedBound(token="yes" if value else "no")
    elif isinstance(value, RateType):
        bound = StatedBound(token=value.value)
    elif isinstance(value, str):
        bound = StatedBound(token=value)
    else:
        return None
    if not statement_matches_excerpt(ScenarioDimension(dimension), bound, excerpt):
        return None
    return bound


def with_statements(
    offer: NormalizedOffer,
    checks: tuple[ScenarioCheck, ...],
    observed_at: datetime,
) -> NormalizedOffer:
    by_dimension = {check.dimension: check for check in checks}
    matches = []
    for match in offer.scenario_matches:
        check = by_dimension.get(match.dimension.value)
        text = match.source_text
        bound = None if text is None else stated_bound(match.dimension.value, text)
        provenance = check.provenance if check is not None and bound is not None else None
        matches.append(match.model_copy(update={"stated": bound, "provenance": provenance}))
    return offer.model_copy(
        update={
            "scenario_matches": tuple(matches),
            "valid_until": observed_at + timedelta(hours=71),
        }
    )
