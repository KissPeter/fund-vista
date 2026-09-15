"""HMAC-signed design tokens for the pen-pixel shop bridge.

The browser hands Woo a ``{design_id, exp, sig}`` triple with its attach
call; Woo verifies the signature with the shared secret, so a design id
can't be forged client-side. The Python service never sees money, prices,
or customer data — it only mints/verifies these tokens.

Algorithm (canonical, mirrored in the shop contract ``docs/backend-contract.md``)::

    sig = hex(HMAC_SHA256(secret, design_id + "|" + exp))

- ``design_id`` reuses the content-addressed ``image_id`` (sha256 hex) —
  no new id scheme.
- ``exp`` is unix epoch seconds; TTL 24 h (``token_ttl_hours`` setting).
- Secret rotation: deploy the new value as ``PENPIXEL_HMAC_SECRET`` and keep
  the old one in ``PENPIXEL_HMAC_SECRET_PREVIOUS`` for a 24 h dual-accept
  window; verification tries current first, then previous.

Only stdlib (hmac/hashlib) — no new dependency. All comparisons are
constant-time (``hmac.compare_digest``).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

log = logging.getLogger(__name__)


def sign_design_token(*, design_id: str, exp: int, secret: str) -> str:
    """Return the hex HMAC-SHA256 over ``design_id + "|" + exp``."""
    msg = f"{design_id}|{exp}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_design_token(
    *,
    design_id: str,
    exp: int,
    sig: str,
    secret: str,
    previous_secret: str = "",
) -> tuple[bool, str]:
    """Verify a token triple.

    Returns ``(valid, reason)`` where ``reason`` is ``ok``,
    ``ok_previous_secret`` (rotation window), or a machine-readable
    failure: ``bad_design_id``, ``bad_expiry``, ``expired``,
    ``bad_signature``, ``signing_unconfigured``.
    """
    if (
        len(design_id) != 64
        or any(c not in "0123456789abcdef" for c in design_id.lower())
    ):
        return False, "bad_design_id"
    if exp <= 0:
        return False, "bad_expiry"
    if not secret:
        return False, "signing_unconfigured"
    candidates = [("ok", secret)]
    if previous_secret and previous_secret != secret:
        candidates.append(("ok_previous_secret", previous_secret))
    for reason, key in candidates:
        expected = sign_design_token(design_id=design_id, exp=exp, secret=key)
        if hmac.compare_digest(expected, sig.lower()):
            if exp < int(time.time()):
                return False, "expired"
            return True, reason
    return False, "bad_signature"
