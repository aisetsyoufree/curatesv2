"""AUD7-4 review: the cited text must really name the charter, the institution and the state.

The text check only screens out blatantly wrong evidence (the human review is the proof), but
it must not be satisfied by lookalikes: a longer charter number, a phone fragment, another
institution whose name merely contains this one, or the letters "ma" inside other words.
"""

import pytest

from . import factories as f


def _review(text: str, name: str = "Example Synthetic Credit Union", charter: int = 12345):  # type: ignore[no-untyped-def]
    return f.identity_review(
        institution_charter=charter,
        legal_name=name,
        evidence=(f.provenance(text),),
    )


@pytest.mark.parametrize(
    ("text", "name", "expected"),
    [
        ("Example Synthetic Credit Union - NCUA charter 12345 - Massachusetts", None, True),
        ("Charter Number: 12345  Example   Synthetic Credit Union", None, True),
        ("EXAMPLE SYNTHETIC CREDIT UNION (Charter # 12345)", None, True),
        ("Example Synthetic Credit Union - NCUA charter 123456", None, False),  # longer number
        ("Example Synthetic Credit Union - NCUA charter 912345", None, False),
        ("Example Synthetic Credit Union call 1-12345-555", None, False),  # no charter keyword
        ("South Example Synthetic Credit Union - charter 12345", None, False),  # prefix
        ("Grace Credit Union - charter 12345", "Ace Credit Union", False),  # substring
        ("Ace Credit Union - charter 12345", "Ace Credit Union", True),
        ("A different credit union - charter 12345", None, False),
        ("30 days", None, False),
    ],
)
def test_identity_evidence_must_name_charter_and_institution(
    text: str, name: str | None, expected: bool
) -> None:
    review = _review(text, name or "Example Synthetic Credit Union")
    assert review.evidence_states_identity is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Located in Massachusetts", True),
        ("MASSACHUSETTS", True),
        ("Boston, MA 02110", True),
        ("State: MA", True),
        ("Ma Bell Credit Union", False),
        ("e-ma-il us", False),
        ("MA degree programs", True),  # the abbreviation token alone is accepted: a screen only
        ("Located in Ohio", False),
        ("mama", False),
        ("SMALL BUSINESS LOANS", False),  # "MA" inside an upper-case word
        ("ROMAN CREDIT UNION EMAIL", False),
        ("Matthew's rates", False),
    ],
)
def test_scope_evidence_must_place_the_institution_in_massachusetts(
    text: str, expected: bool
) -> None:
    assert _review(text).evidence_states_massachusetts is expected
