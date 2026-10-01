"""Shared book/id conventions for this repo.

We use a consistent `book_prefix` format, e.g. `math_9a_rjb`, and derived ids like
`math_9a_rjb_ch1_s2`. Multiple modules (kg merge, benchmark generation, etc.)
need the same book-code ordering and regex parsing, so keep them centralized here.
"""

from __future__ import annotations

import re
from typing import Final

BOOK_CODE_ORDER: Final[list[str]] = [
    "1a",
    "1b",
    "2a",
    "2b",
    "3a",
    "3b",
    "4a",
    "4b",
    "5a",
    "5b",
    "6a",
    "6b",
    "7a",
    "7b",
    "8a",
    "8b",
    "9a",
    "9",
    "9b",
    "bx1",
    "bx2",
    "bx3",
    "xzxbx1",
    "xzxbx2",
    "xzxbx3",
]

BOOK_CODE_ORDER_INDEX: Final[dict[str, int]] = {code: idx for idx, code in enumerate(BOOK_CODE_ORDER)}

PRIMARY_MATH_BOOK_CODES: Final[set[str]] = {"1a", "1b", "2a", "2b", "3a", "3b", "4a", "4b", "5a", "5b", "6a", "6b"}
MIDDLE_SCHOOL_BOOK_CODES: Final[set[str]] = {"7a", "7b", "8a", "8b", "9a", "9", "9b"}
HIGH_SCHOOL_BOOK_CODES: Final[set[str]] = {"bx1", "bx2", "bx3", "xzxbx1", "xzxbx2", "xzxbx3"}

BOOK_CODES: Final[set[str]] = PRIMARY_MATH_BOOK_CODES | MIDDLE_SCHOOL_BOOK_CODES | HIGH_SCHOOL_BOOK_CODES

BOOK_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<subject>[a-z]+)_(?P<book_code>"
    r"1a|1b|2a|2b|3a|3b|4a|4b|5a|5b|6a|6b|7a|7b|8a|8b|9a|9|9b|"
    r"bx1|bx2|bx3|xzxbx1|xzxbx2|xzxbx3"
    r")_rjb$"
)

TYPE_CODE_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<subject>[a-z]+)_(?P<book_code>"
    r"1a|1b|2a|2b|3a|3b|4a|4b|5a|5b|6a|6b|7a|7b|8a|8b|9a|9|9b|"
    r"bx1|bx2|bx3|xzxbx1|xzxbx2|xzxbx3"
    r")_rjb(?:_|$)"
)

CHAPTER_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<subject>[a-z]+)_(?P<book_code>"
    r"1a|1b|2a|2b|3a|3b|4a|4b|5a|5b|6a|6b|7a|7b|8a|8b|9a|9|9b|"
    r"bx1|bx2|bx3|xzxbx1|xzxbx2|xzxbx3"
    r")_rjb_ch(?P<chapter>\d+)$"
)

SECTION_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<subject>[a-z]+)_(?P<book_code>"
    r"1a|1b|2a|2b|3a|3b|4a|4b|5a|5b|6a|6b|7a|7b|8a|8b|9a|9|9b|"
    r"bx1|bx2|bx3|xzxbx1|xzxbx2|xzxbx3"
    r")_rjb_ch(?P<chapter>\d+)_s(?P<section>\d+)$"
)


# Some internal/older working copies spell high-school books as
# ``math_highschool_rjb_bx1`` instead of ``math_bx1_rjb``. Everything public uses
# the latter, so entry points normalise with :func:`normalize_book_prefix`.
_STAGED_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<subject>[a-z]+)_(?:primaryschool|middleschool|highschool)_rjb_(?P<code>.+)$"
)


def normalize_book_prefix(book_prefix: str) -> str:
    """Return the canonical ``<subject>_<code>_rjb`` form of *book_prefix*.

    ``math_highschool_rjb_bx1`` -> ``math_bx1_rjb``; anything already canonical
    (or unrecognised) is returned unchanged.
    """
    match = _STAGED_PREFIX_RE.match(book_prefix or "")
    if match:
        return f"{match.group('subject')}_{match.group('code')}_rjb"
    return book_prefix


def section_node_id(book_prefix: str, section_id: str) -> str:
    """Node id of a section, matching ``src/kg/merge_kg.py`` (``<book>_ch1_s2``)."""
    return f"{normalize_book_prefix(book_prefix)}_{section_id}"
