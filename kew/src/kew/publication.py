"""Detached host-publication credentials and safe failure evidence."""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import urlsplit, urlunsplit

from kew.credential_surface import redact_credential_values
from kew.repo_host import repo_host
from kew.runner_fields import runner_text
from kew.subprocess_utils import TOOL_PATHS, run

if TYPE_CHECKING:
    from kew.run_store import RunStore

PublicationStep = Literal["commit", "push", "pr_create", "unknown"]
PublicationMode = Literal["canonical", "checkout"]
PUBLICATION_CONFIGURATION_REFUSAL = "publication_configuration_refused"

# The driver resolves stored ``gh`` auth while it still has its foreground
# credential surface.  The detached child carries that capability under a
# kew-owned name so the agent environment can exclude it.
DETACHED_PUBLICATION_TOKEN_ENV = "KEW_DETACHED_PUBLICATION_TOKEN"  # nosec B105

_DETAIL_LIMIT = 500
_GITHUB_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,})\b",
    re.IGNORECASE,
)
_AUTH_SCHEME_RE = re.compile(r"(?i)\b(?:basic|bearer)\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(authorization|password|passwd|token|secret|api[_-]?key)"
    r"(\s*[:=]\s*|\s+)([^\s,;]+)"
)
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# A file URI is an absolute path wearing a scheme — with or without an
# authority, one slash or three. Unwrapping it lets the one path rule judge it
# instead of teaching a second rule about URIs.
_FILE_URI_RE = re.compile(r"(?i)\bfile:(?://[^/\s]*)?(?=/)")
# An encoded separator is still a separator; decoding it lets every later
# rule see the text it was written to judge.
_ENCODED_SLASH_RE = re.compile(r"(?i)%2F")
_ENCODED_PERCENT_RE = re.compile(r"(?i)%25")
# A slash run is one separator. The colon guard keeps scheme remnants such as
# ``https://host`` intact — their double slash follows a colon.
_SLASH_RUN_RE = re.compile(r"(?<!:)/{2,}")
# Git quotes the paths it reports, and a quoted path may hold spaces the token
# rule below cannot claim — so a quoted span is judged whole, first. One
# branch per quote character, so the OTHER quote may appear inside the path
# (``"/Users/x/it's odd/f.txt"``).
_QUOTED_PATH_RE = re.compile(
    r"'((?:/|~(?:[A-Za-z_][\w.-]*)?/)[^']*)'"
    + r'|"((?:/|~(?:[A-Za-z_][\w.-]*)?/)[^"]*)"'
)
# The token boundary keeps sanitized URL segments intact: in
# ``https://host/path`` every slash follows a colon, a slash, or a word
# character, so none can start a match. What the boundary admits is the
# ``word:/abs/path`` shape (fatal:, sqlite:, host:, C:). Home prefixes are
# ancestry whether spelled ``~/``, ``~name/``, ``$HOME/`` or ``${HOME}/``.
# Lone POSIX names such as ``/etc`` are ambiguous prose and stay; an unquoted
# path with spaces keeps its post-space suffix — the leading ancestry, where
# usernames live, is still always redacted.
# ``~`` names a home only when followed by ``/`` or a POSIX-style username
# (letter or underscore first) — ``~5/10`` and ``~=3/4`` are prose fractions,
# not ancestry.
_ABS_PATH_RE = re.compile(
    r"(?<![\w@./])"
    r"(?:/(?:[^\s/]+/)+|(?:~(?:[A-Za-z_][\w.-]*)?|\$\{?HOME\}?)/(?:[^\s/]+/)*)"
    r"([^\s/]+)"
)


def _redact_quoted_path(match: re.Match[str]) -> str:
    if match.group(1) is not None:
        quote, path = "'", match.group(1)
    else:
        quote, path = '"', match.group(2)
    segments = [part for part in path.split("/") if part]
    basename = segments[-1] if segments else ""
    return f"{quote}<path>/{basename}{quote}"


class PublicationError(RuntimeError):
    """A host publication failure bound to its exact terminal step."""

    def __init__(
        self,
        step: PublicationStep,
        detail: object,
        *,
        host_paths: tuple[Path, ...] = (),
    ) -> None:
        self.step = step
        self.detail = sanitize_publication_error(detail, host_paths=host_paths)
        super().__init__(self.detail)


# A rejection the remote decided from THIS publication's content or lineage.
# Retrying one re-sends the same immutable commit against the same decision, so
# the answer cannot change: a protected ref, a lost force-with-lease race, a
# non-fast-forward, a push rule or a secret-scanning refusal. Checked before the
# transient markers below, because a settled rejection also carries the generic
# "remote rejected" line (#2455).
_SETTLED_PUSH_REJECTIONS = (
    "non-fast-forward",
    "fetch first",
    "stale info",
    "protected branch",
    "refusing to allow",
    "gh006",
    "gh013",
    "repository rule violation",
    "secret scanning",
    "gitleaks",
    "push declined",
    "would clobber existing tag",
    "shallow update not allowed",
    "deny updating a hidden ref",
)

# A rejection the remote failed on its OWN side: the push said nothing wrong,
# the server did not answer it. #2455 is the incident -- GitHub answered a
# review-remediation push with "remote: Internal Server Error", the run was
# failed, and the epic loop redid a 97-step fix from scratch instead of pushing
# the work it already had. Resending the same commit is the right move.
#
# "returned error: 5" is git's HTTP transport reporting a 5xx status; a bare
# three-digit number is never matched on its own, because such a run is also a
# commit-sha substring.
_TRANSIENT_PUSH_REJECTIONS = (
    "server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "gateway time-out",
    "returned error: 5",
    "remote rejected",
    "remote unpack failed",
    "unpack failed",
    "remote end hung up",
    "connection reset",
    "connection timed out",
    "operation timed out",
    "early eof",
    "rpc failed",
    "temporarily unavailable",
    "try again",
)


def transient_push_rejection(detail: object) -> bool:
    """Whether a push failure is the remote's own, so resending it may work.

    Unknown text is NOT transient. A retry of an unrecognized rejection buys a
    wait and the same answer, while the work is preserved either way -- so the
    default errs toward reporting the failure instead of sitting on it.
    """
    text = str(detail).lower()
    if any(marker in text for marker in _SETTLED_PUSH_REJECTIONS):
        return False
    return any(marker in text for marker in _TRANSIENT_PUSH_REJECTIONS)


class TransientPushFailure(PublicationError):
    """A server-side push failure that survived every retry (#2455).

    Carried as its own type so the review publication path can tell it from a
    broken push: the commit is built, the checkout is preserved and nothing
    about the work needs redoing -- only the push has to happen again. Read
    through the cause chain, because the canonical path re-raises a push
    failure wrapped for its callers.
    """

    def __init__(self, detail: object, *, attempts: int) -> None:
        super().__init__("push", detail)
        self.attempts = attempts


def select_publication_mode(
    value: object,
    *,
    dangerously_skip_containment: bool = False,
    store: RunStore | None = None,
    run_id: str | None = None,
    issue_num: int | None = None,
    item_id: str | None = None,
    branch: str = "",
    repo: str | None = None,
) -> PublicationMode:
    """Validate publication mode and record its effective security posture."""
    if value not in ("canonical", "checkout"):
        raise PublicationError("push", f"unknown publication mode {value!r}")
    mode = cast(PublicationMode, value)
    if mode == "checkout" and not dangerously_skip_containment:
        raise PublicationError(
            "push",
            "checkout publication is refused by #1218 because it has "
            "uncontained-publication posture; set [sandbox] "
            "dangerously_skip_containment = true to admit this host risk",
        )
    posture = (
        "uncontained-publication" if mode == "checkout" else "canonical-publication"
    )
    if mode == "checkout":
        from kew.progress import _warn

        _warn("Publication uses the temporary checkout escape hatch.")
    if store is not None:
        from kew.audit import Actor, emit

        emit(
            store,
            "publication_posture",
            actor=Actor("policy", "host-publication"),
            run_id=run_id,
            issue_num=issue_num,
            item_id=item_id,
            repo=repo,
            payload={"branch": branch, "posture": posture},
            dedup_key=(f"publication-posture:v1:{run_id}:{branch}" if run_id else None),
        )
    return mode


def capture_detached_publication_token() -> str | None:
    """Resolve GitHub auth before ``setsid`` removes the controlling terminal.

    Explicit token auth wins. Otherwise ask the already-preflighted ``gh`` CLI
    to read its credential store while the epic driver is still foreground.
    Failure is fail-soft: the detached child retains the legacy environment and
    will produce durable publication diagnostics if authentication later fails.
    """
    for name in (DETACHED_PUBLICATION_TOKEN_ENV, "GH_TOKEN", "GITHUB_TOKEN"):
        token = _valid_token(os.environ.get(name))
        if token is not None:
            return token
    if "gh" not in TOOL_PATHS:
        return None
    try:
        result = run(["gh", "auth", "token"], check=False, timeout=10)
    except Exception:  # noqa: BLE001 — optional credential stabilization
        return None
    if result.returncode != 0:
        return None
    return _valid_token(result.stdout)


def activate_detached_publication_token() -> None:
    """Expose the captured token to host control-plane subprocesses."""
    token = _valid_token(os.environ.get(DETACHED_PUBLICATION_TOKEN_ENV))
    if token is not None:
        os.environ["GH_TOKEN"] = token


def publication_subprocess_env() -> dict[str, str] | None:
    """Return an environment with captured detached auth promoted for ``gh``."""
    token = _valid_token(os.environ.get(DETACHED_PUBLICATION_TOKEN_ENV))
    if token is None:
        return None
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    return env


def publication_git_env(*, host: str | None = None) -> dict[str, str] | None:
    """Return non-interactive HTTPS git auth without placing a token in argv."""
    env = publication_subprocess_env()
    if env is None:
        return None
    token = env["GH_TOKEN"]
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    if count < 0 or count > 100:
        count = 0
    from kew.config import project_root

    if host is None:
        host = repo_host(project_root())
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    env[f"GIT_CONFIG_KEY_{count}"] = f"http.https://{host}/.extraHeader"
    env[f"GIT_CONFIG_VALUE_{count}"] = f"Authorization: Basic {encoded}"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def detached_child_env(token: str | None) -> dict[str, str] | None:
    """Snapshot the driver environment only when a credential was captured."""
    valid = _valid_token(token)
    if valid is None:
        return None
    env = dict(os.environ)
    env[DETACHED_PUBLICATION_TOKEN_ENV] = valid
    return env


def publication_failure(exc: Exception) -> tuple[PublicationStep, str]:
    """Return stable, secret-free evidence for a publication exception."""
    if isinstance(exc, PublicationError):
        return exc.step, exc.detail
    detail = sanitize_publication_error(exc)
    lowered = detail.lower()
    # Most specific marker first: a failed push commonly quotes commit text in
    # its hint output, so a generic "commit" match would mislabel the step.
    if "pr create" in lowered or "pull request" in lowered:
        step: PublicationStep = "pr_create"
    elif "push" in lowered:
        step = "push"
    elif "commit" in lowered:
        step = "commit"
    else:
        step = "unknown"
    return step, detail


def sanitize_publication_error(
    value: object, *, host_paths: tuple[Path, ...] = ()
) -> str:
    """Bound provider output and remove credentials, URL secrets, and controls.

    Deliberately NOT path-redacting: operator-facing failures name the
    preserved checkout so the work can be salvaged by hand. Evidence that
    outlives the host uses :func:`sanitize_publication_evidence` instead.

    ``host_paths`` are kew-owned paths the message names. The filesystem
    bounds them, so they do not spend the provider-output budget: a long
    state directory must not cut off the action written after the path.
    """
    limit = _DETAIL_LIMIT + sum(len(str(path)) for path in host_paths)
    text = _publication_text(value)
    text = redact_credential_values(
        _URL_RE.sub(lambda match: _sanitize_url(match.group(0)), text)
    )
    text = _AUTH_SCHEME_RE.sub("<redacted-auth>", text)
    text = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = _GITHUB_TOKEN_RE.sub("<redacted>", text)
    return (redact_credential_values(text) or "publication command failed")[:limit]


def _publication_text(value: object) -> str:
    """Preserve header boundaries, then normalize and check current credentials."""
    text = runner_text(value)
    text = redact_credential_values(_CONTROL_RE.sub(" ", text))
    return redact_credential_values(" ".join(text.split()))


def sanitize_publication_evidence(
    value: object, *, limit: int | None = _DETAIL_LIMIT
) -> str:
    """Sanitize for the audit ledger: the error rules plus path ancestry.

    The basename is the useful bounded evidence — which file was involved;
    the ancestry, where usernames and host layout live, is the leak. Applied
    only at the audit-evidence boundary, never to operator-facing errors,
    which must keep naming the preserved checkout.

    Encoded separators are decoded before ANY judging rule runs: a rule can
    only refuse what it can see, and a %2F splice would otherwise hide a
    secret assignment from the very rule written to catch it. The loop is
    bounded and handles nested encodings (%252F).
    """
    text = _publication_text(value)
    for _ in range(3):
        decoded = redact_credential_values(_ENCODED_PERCENT_RE.sub("%", text))
        decoded = redact_credential_values(_ENCODED_SLASH_RE.sub("/", decoded))
        if decoded == text:
            break
        text = decoded
    text = redact_credential_values(
        _URL_RE.sub(lambda match: _sanitize_url(match.group(0)), text)
    )
    text = _AUTH_SCHEME_RE.sub("<redacted-auth>", text)
    text = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = _GITHUB_TOKEN_RE.sub("<redacted>", text)
    text = redact_credential_values(_FILE_URI_RE.sub("", text))
    text = redact_credential_values(_SLASH_RUN_RE.sub("/", text))
    text = redact_credential_values(_QUOTED_PATH_RE.sub(_redact_quoted_path, text))
    text = redact_credential_values(_ABS_PATH_RE.sub(r"<path>/\1", text))
    # Callers retaining output tails must sanitize the full text before slicing.
    return (text or "publication command failed")[:limit]


def append_publication_failure(
    log_file: Path,
    *,
    step: PublicationStep,
    category: str,
    detail: str,
) -> None:
    """Append the supervisor-owned terminal publication fact to the run log."""
    line = (
        f"[kew] publication failed step={step} category={category} "
        f"error={sanitize_publication_error(detail)}\n"
    )
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        return


def _valid_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not token or len(token) > 4096 or any(char.isspace() for char in token):
        return None
    return token


def _sanitize_url(value: str) -> str:
    trailing = ""
    while value and value[-1] in ".,;:)]}":
        trailing = value[-1] + trailing
        value = value[:-1]
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        sanitized = urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except (TypeError, ValueError):
        sanitized = "<redacted-url>"
    return sanitized + trailing
