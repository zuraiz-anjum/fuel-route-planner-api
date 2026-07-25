"""Canonicalizes a free-text location query for cache-key purposes.

Without this, trivially different formatting of the *same* input:
"Chicago, IL" vs "Chicago,IL" vs "Chicago,   IL", hashes to different
cache keys, which meant equivalent-to-a-human queries could each trigger
their own external geocoding/routing call and their own persisted
RoutePlan row instead of sharing one. This is purely a cache-key concern;
it never changes what's actually sent to a geocoder (which already handles
these variants fine on its own).
"""

import re

_WHITESPACE_RUN = re.compile(r"\s+")
_COMMA_SPACING = re.compile(r"\s*,\s*")


def normalize_query(query: str) -> str:
    collapsed = _WHITESPACE_RUN.sub(" ", query.strip().lower())
    return _COMMA_SPACING.sub(", ", collapsed)
