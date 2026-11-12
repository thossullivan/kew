"""The nodesource apt signing key every agent image trusts."""

from __future__ import annotations

#: SHA-256 of https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key,
#: verified on 2026-11-02 against the key's fingerprint
#: 6F71 F525 2828 41EE DAF8 51B4 2F59 B5F9 9B1B E0B4
#: ("NSolid <nsolid-gpg@nodesource.com>"). Every agent Dockerfile checks the
#: downloaded key against this digest before apt trusts it; change it only after
#: re-verifying a new key's fingerprint the same way.
NODESOURCE_KEY_SHA256 = (
    "b42e0321dabdc24e892115da705cf061167eac12a317f23d329862d0aa0a271d"
)
