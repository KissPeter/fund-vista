"""Readable-TLS-CA guard for outbound HTTP clients (fix-once home).

httpx builds its TLS trust context EAGERLY at ``httpx.AsyncClient(...)`` /
``httpx.Client(...)`` — it reads the certifi ``cacert.pem`` off disk before
any network I/O (httpx ``_config.SSLConfig.load_ssl_context_verify``). When
that file is unreadable at the OS level, every outbound HTTPS call fails
the same way before a byte is sent. Observed 2026-10-20 on the
``pen-pixel.hu`` deploy:

    [Errno 5] Input/output error:
    '/usr/local/lib/python3.13/site-packages/certifi/cacert.pem'

(a corrupted image layer in the container filesystem), surfacing as raw
502 ``ourairports_unavailable`` on OurAirports cache-miss lookups and
equally breaking the kh.hu/Erste/frankfurter proxy, Overpass, Nominatim
and the traffic archive fetch. Warm Redis caches (24 h TTL) masked it.

:func:`async_client` and :func:`client` probe the certifi bundle first and,
when it is unreadable, build the client against the first readable system
CA bundle (``SSL_CERT_FILE``, then the usual distro paths). TLS stays
verified — verification is never disabled. When no bundle can be read no
override is injected, so the constructor raises the original OSError and
routers keep mapping it to their 502 envelopes. :func:`ca_status` exposes
the probe result for ``/healthz``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import certifi
import httpx

log = logging.getLogger("fund-vista.http.tls")

# First readable one wins, in order: explicit env override, then the
# bundle the common base images ship (the image is Debian-family),
# then the rest of the usual suspects.
_FALLBACK_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian/Ubuntu/official python images
    "/etc/ssl/cert.pem",  # Alpine + macOS system bundle
    "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora/RHEL
    "/etc/ssl/ca-bundle.pem",  # openSUSE
)


def _readable(path: str) -> bool:
    """Byte-read probe: a corrupted/unreadable file fails here, not later."""
    try:
        with open(path, "rb") as fh:
            fh.read(1)
    except OSError:
        return False
    return True


def probe_ca() -> dict[str, str]:
    """Probe certifi's bundle; return ``/healthz``-ready status fields."""
    path = certifi.where()
    status = {"certifi_path": path, "certifi_readable": "yes"}
    if _readable(path):
        return status
    status["certifi_readable"] = "no"
    candidates = []
    env_bundle = os.environ.get("SSL_CERT_FILE") or ""
    if env_bundle and _readable(env_bundle):
        candidates.append(env_bundle)
    candidates += [bundle for bundle in _FALLBACK_BUNDLES if _readable(bundle)]
    if candidates:
        status["fallback_bundle"] = candidates[0]
        log.warning(
            "TLS CA bundle unreadable at %s (filesystem-level error; "
            "corrupted image layer?) — outbound HTTPS falls back to the "
            "system bundle %s. Rebuild/redeploy the image (or set "
            "SSL_CERT_FILE) to retire the fallback.",
            path, candidates[0],
        )
    return status


def _guarded_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Inject ``verify=fallback`` only when needed and the caller didn't say."""
    status = probe_ca()
    if status["certifi_readable"] == "yes":
        return kwargs
    fallback = status.get("fallback_bundle")
    if fallback and kwargs.get("verify", True) is True:
        # Caller-supplied verify (False / SSLContext / explicit path) wins;
        # True and absent both mean "use the default bundle" → guarded.
        return {**kwargs, "verify": fallback}
    # Nothing readable: hand construction untouched so httpx raises its
    # (original, informative) OSError for the caller's error mapping.
    return kwargs


def async_client(**kwargs: Any) -> httpx.AsyncClient:
    """``httpx.AsyncClient`` guarded against an unreadable certifi bundle."""
    return httpx.AsyncClient(**_guarded_kwargs(kwargs))


def client(**kwargs: Any) -> httpx.Client:
    """``httpx.Client`` guarded against an unreadable certifi bundle."""
    return httpx.Client(**_guarded_kwargs(kwargs))


def ca_status() -> dict[str, str]:
    """Current probe result for ``/healthz`` (see :func:`probe_ca`)."""
    return probe_ca()


__all__ = ["async_client", "ca_status", "client"]
