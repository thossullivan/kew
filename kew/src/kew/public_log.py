"""Job log text fit for a PUBLIC GitHub issue (#3195).

The nightly files regression and scan-failure issues that quote the verify
job's log. Those issues are public, so every line is cleaned here first, and
the whole window is cleaned before anything is sliced out of it: a line's
meaning can depend on the lines before it (a PEM body is base64 that only its
``BEGIN`` line marks as a key).

On top of :func:`kew.publication.sanitize_publication_evidence` — credentials
kew knows, URL secrets, ``KEY=value`` assignments, host path ancestry — it:

* drops every line of a PEM block, and any base64-only line of 40 or more
  characters (a PEM body whose ``BEGIN`` fell outside the window);
* redacts Slack tokens (``xoxb-…``, ``xoxp-…`` and the rest of the family);
* redacts the user information of a URL in any scheme
  (``postgres://admin:hunter2@db``, ``redis://``, ``amqp://``);
* redacts ``*.internal`` host names, which on GCP carry the project id
  (``vm.c.<project>.internal``);
* redacts a whole GCP resource path (``projects/<id>/zones/…``), which names
  the project, zone and instance, and a ``gs://`` bucket name;
* redacts ``user@host`` and email addresses, service accounts among them;
* redacts IPv6 addresses, a zone index (``fe80::1%ens4``) included;
* redacts this machine's host name, which an operator's lease token carries.
"""

from __future__ import annotations

import re
import socket
from collections.abc import Iterable

#: What stands in for a dropped PEM block.
PEM_REDACTED = "<redacted PEM block>"

_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]+-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]+-----")
_BASE64_LINE = re.compile(r"^[A-Za-z0-9+/_-]{40,}={0,2}$")
_SLACK_TOKEN = re.compile(r"\bxox[a-z]-[A-Za-z0-9-]+")
_URL_USERINFO = re.compile(r"\b([A-Za-z][A-Za-z0-9+.-]*://)[^\s/@]+@")
_INTERNAL_HOST = re.compile(r"\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.internal\b")
#: ``projects/<id>/`` then a GCP collection: a ``tests/projects/test_x.py``
#: path in a failing test's id is not one.
_GCP_RESOURCE = re.compile(
    r"\bprojects/[^/\s'\"]+/(?:zones|regions|locations|global|instances|"
    r"serviceAccounts|secrets|topics|subscriptions|buckets|databases)\b[^\s'\"]*"
)
_GCS_BUCKET = re.compile(r"\bgs://[^/\s'\"]+")
#: ``user@host`` and ``name@domain``. The host starts with a letter, so a
#: package pinned to a version (``react@18.3.1``) is not one, and carries a
#: ``.``, ``-`` or digit or ends at ssh's ``:``, so ``obj@property`` is not.
_ADDRESS = re.compile(r"\b[\w.+-]+@(?:[A-Za-z][\w-]*[.\d-][\w.-]*|[A-Za-z][\w-]*(?=:))")
_HEX = r"[0-9A-Fa-f]{1,4}"
#: An IPv6 address written in full, or compressed with ``::`` between two
#: groups (a clock time such as ``12:34:56`` is neither), with a zone index.
#: A ``:`` may follow it when no group does, as in ssh's ``user@host: …``.
_IPV6 = re.compile(
    rf"(?<![\w:.])(?:(?:{_HEX}:){{7}}{_HEX}|(?:{_HEX}:){{1,6}}(?::{_HEX}){{1,6}})"
    r"(?:%[\w.-]+)?(?!\w|:[\w:])"
)
#: ``user@`` before an address already redacted to ``<ip>``.
_USER_AT_IP = re.compile(r"[\w.+-]+@<ip>")
#: Host names shorter than this are not redacted by name: too likely a word.
_MIN_HOST_NAME = 4

#: How long one cleaned line may be.
LINE_CHARS = 300


def public_line(text: str, *, limit: int = LINE_CHARS) -> str:
    """One line, cleaned for a public issue."""
    from kew.publication import sanitize_publication_evidence

    text = _SLACK_TOKEN.sub("<redacted-slack-token>", text)
    text = _URL_USERINFO.sub(lambda match: f"{match.group(1)}<redacted>@", text)
    text = _INTERNAL_HOST.sub("<internal-host>", text)
    text = _GCP_RESOURCE.sub("<gcp-resource>", text)
    text = _GCS_BUCKET.sub("gs://<bucket>", text)
    # IPv6 first: ``tom@fe80::1`` would otherwise lose only ``tom@fe80``.
    text = _IPV6.sub("<ip>", text)
    text = _USER_AT_IP.sub("<address>", text)
    text = _ADDRESS.sub("<address>", text)
    text = _without_host_name(text)
    return sanitize_publication_evidence(text, limit=limit)


def _without_host_name(text: str) -> str:
    """*text* with this machine's host name, in each form a lease token or a
    prompt prints it, replaced."""
    raw = socket.gethostname()
    forms = {
        raw,
        re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-"),
        raw.split(".", 1)[0],
    }
    for name in sorted(forms, key=len, reverse=True):
        if len(name) < _MIN_HOST_NAME or name.lower() == "localhost":
            continue
        text = re.sub(
            rf"(?<![\w.-]){re.escape(name)}(?![\w-])", "<operator-host>", text
        )
    return text


def public_lines(lines: Iterable[str], *, limit: int = LINE_CHARS) -> list[str]:
    """Every line of *lines* cleaned, PEM blocks and base64 runs dropped.

    Call it on the whole window before slicing a tail out of the result.
    """
    cleaned: list[str] = []
    in_pem = False
    for line in lines:
        if _PEM_BEGIN.search(line):
            in_pem = not _PEM_END.search(line)
            cleaned.append(PEM_REDACTED)
            continue
        if in_pem or _PEM_END.search(line):
            in_pem = in_pem and not _PEM_END.search(line)
            continue
        if _BASE64_LINE.match(line.strip()):
            continue
        cleaned.append(public_line(line, limit=limit))
    return cleaned
