"""Thin re-export shim (audit F1).

Canonical Ed25519 identity primitives live in the top-level
``nexus_crypto`` leaf package so ``relay/`` can import them without
depending on ``app/``. This module re-exports everything so all existing
``app.identity.crypto`` importers keep working unchanged.
"""

from __future__ import annotations

from nexus_crypto import *  # noqa: F401,F403
from nexus_crypto import __all__  # noqa: F401  (re-exported so star-imports keep working)
