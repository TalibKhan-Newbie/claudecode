"""creator-contacts — published business-contact discovery for mid-size creators.

Scope is deliberately narrow: contact details that creators publish so that
brands can reach them. See ``docs/SCOPE.md`` for what is in and what is out.
"""

__version__ = "0.1.0"

from .models import (  # noqa: F401
    ContactKind,
    ContactPoint,
    Creator,
    Evidence,
    Platform,
    SourceType,
)

__all__ = [
    "ContactKind",
    "ContactPoint",
    "Creator",
    "Evidence",
    "Platform",
    "SourceType",
    "__version__",
]
