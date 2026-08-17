from __future__ import annotations

import hashlib


def promotion_code_hashes(code: str) -> tuple[str, str]:
    """Return the current canonical hash and the pre-FCX legacy hash.

    CAD promotion redemption historically accepted a code hashed with its
    punctuation intact as well as the compact hash.  Migrated campaigns can
    therefore legitimately contain either representation.
    """
    entered = "".join(str(code or "").upper().split())
    canonical = "".join(character for character in entered if character.isalnum())
    return (
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        hashlib.sha256(entered.encode("utf-8")).hexdigest(),
    )
