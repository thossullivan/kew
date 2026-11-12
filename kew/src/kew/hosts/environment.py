"""The job environment is an allowlist, and credential names never pass it.

Three rules, all enforced here so every provider inherits them:

* Nothing is inherited from kew's own environment except the fixed, named,
  non-credential variables in :data:`AMBIENT_ALLOWLIST`. A job gets what it
  declared and what a shell needs to run at all.
* A declared variable whose NAME looks like a credential is refused, with the
  name in the message — even when the caller asks for it. The drive's operator
  scripts carried this rule as a comment; here it is a check.
* A declared variable must be *named* like a variable.
  :class:`~kew.hosts.gcp.GcpProvider` builds an ``env NAME=value`` prefix for
  the host's shell, and a name — unlike a value — is not a word a shell lets
  you quote, so ``PATH;curl evil`` is refused here rather than escaped there.

What a job *does* get on a remote host is a PATH: :data:`REMOTE_PATH`, the
recipe's own install locations plus the system directories, prepended by
:func:`remote_path_prefix`. A remote job arrives in a non-login shell that
sources no profile, so without it a host that provisioned cleanly failed every
job with ``uv: not found`` (#2433).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from kew.hosts.errors import CredentialVariableRefused, VariableNameRefused

#: Names kew forwards from its own environment when present. Fixed and small:
#: a shell needs a PATH, a git clone needs a HOME to find its config, and the
#: rest only decide how output is formatted. None of them is a credential.
#:
#: This is a *local*-host allowlist. A remote host has its own PATH and HOME;
#: forwarding this machine's would point it at directories that do not exist
#: there, so :class:`~kew.hosts.gcp.GcpProvider` passes only the declared
#: variables and gives the remote shell :data:`REMOTE_PATH` instead. It cannot
#: wait for a login shell to supply one: a job arrives as a ``--command``,
#: which no profile is sourced for (#2433).
AMBIENT_ALLOWLIST: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")

#: The PATH a job runs under on a *remote* host, in order. The first two
#: entries are where the provisioning recipe puts the tools it installs that
#: apt did not — uv lands in ``/usr/local/bin``, a curl-installed tool in
#: ``$HOME/.local/bin`` — and only a *login* shell's profile puts the second
#: of those on PATH. A remote job is not a login shell: it arrives over
#: ``gcloud compute ssh --command``, which sources no profile, and kew
#: deliberately forwards no ambient PATH to a host whose directories are not
#: this machine's. Nothing, therefore, put the recipe's own install locations
#: on PATH, and ``kew verify --host gcp`` died with ``uv: not found`` and exit
#: 127 in 1.6 seconds (#2433). The remaining entries are the standard system
#: directories, named rather than assumed, so the PATH a job gets is known
#: even when the host's shell starts with none at all.
#:
#: ``$HOME`` is expanded by the *host's* shell, not here: it is the job's own
#: home, which kew does not know and must not guess.
REMOTE_PATH_ENTRIES: tuple[str, ...] = (
    "$HOME/.local/bin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/usr/bin",
    "/usr/sbin",
    "/bin",
    "/sbin",
)

#: :data:`REMOTE_PATH_ENTRIES` as a shell PATH value. The recipe pins the same
#: list as its ``KEW_JOB_PATH`` default, and
#: ``tests/test_hosts_provision.py`` refuses the two to drift.
REMOTE_PATH = ":".join(REMOTE_PATH_ENTRIES)

#: Anything containing one of these is credential-shaped (`*TOKEN*`, `*KEY*`,
#: `*SECRET*`), whatever it is prefixed or suffixed with.
CREDENTIAL_SUBSTRINGS: tuple[str, ...] = ("TOKEN", "KEY", "SECRET", "PASSWORD")

#: Whole credential namespaces: a host gets nothing from the forge, the
#: tracker, the cloud SDK, or any model provider.
CREDENTIAL_PREFIXES: tuple[str, ...] = (
    "GH_",
    "GITHUB_",
    "LINEAR_",
    "ANTHROPIC_",
    "OPENAI_",
    "OPENROUTER_",
    "GOOGLE_",
    "CLOUDSDK_",
)


#: What a job variable may be named: the portable character set, which is
#: also the set every shell accepts in an assignment. Anchored at both ends,
#: so a name carrying a space, a quote or a ``;`` matches nothing.
VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def remote_path_prefix() -> str:
    """A shell prefix that gives a remote command a known PATH.

    Prepended, not substituted: whatever PATH the host's own shell starts with
    is kept after kew's entries, so a host with something extra on it keeps it,
    while a host with nothing on it still finds every tool the recipe
    installed. ``${PATH:+:$PATH}`` is what makes an *empty* inherited PATH
    append nothing — a bare ``:$PATH`` would leave a trailing empty entry,
    which a shell reads as the current directory, and the current directory of
    a job is the shipped checkout.
    """
    return f'PATH="{REMOTE_PATH}${{PATH:+:$PATH}}"; export PATH; '


def refuse_unusable_name(name: str) -> None:
    """Raise unless *name* is a name a shell would accept in an assignment."""
    if not VARIABLE_NAME.match(name):
        raise VariableNameRefused(name)


def credential_shape(name: str) -> str | None:
    """Why *name* reads as a credential, or ``None`` when it does not."""
    upper = name.upper()
    for prefix in CREDENTIAL_PREFIXES:
        if upper.startswith(prefix):
            return f"its name is in the {prefix}* credential namespace"
    for fragment in CREDENTIAL_SUBSTRINGS:
        if fragment in upper:
            return f"its name contains {fragment}"
    return None


def refuse_credentials(requested: Mapping[str, str]) -> dict[str, str]:
    """Return *requested* unchanged, or raise naming the first refused variable.

    Both name rules are applied here, because this is the one gate every
    provider passes a declared environment through: a name that is not a
    variable name is refused first, then a name that reads as a credential.
    Names are checked in sorted order so a caller passing several credentials
    always hears about the same one first.
    """
    for name in sorted(requested):
        refuse_unusable_name(name)
        reason = credential_shape(name)
        if reason is not None:
            raise CredentialVariableRefused(name, reason)
    return dict(requested)


def job_environment(
    requested: Mapping[str, str],
    *,
    ambient: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The complete environment a job runs under: allowlist plus declarations.

    *ambient* defaults to ``os.environ`` and contributes only the
    :data:`AMBIENT_ALLOWLIST` names it actually has. Declared variables are
    credential-checked first, so a refusal happens before anything is built.
    """
    declared = refuse_credentials(requested)
    source = os.environ if ambient is None else ambient
    environment = {name: source[name] for name in AMBIENT_ALLOWLIST if name in source}
    environment.update(declared)
    return environment
