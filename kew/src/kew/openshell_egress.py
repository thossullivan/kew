"""The live OpenShell egress probe behind ``kew sandbox certify``.

Admission on ``[sandbox] runtime = "openshell"`` claims the container boundary
holds. This module is what earns that claim: it creates a throwaway sandbox
from the runner's immutable image ID and the runner's own OpenShell policy,
proves from inside it that the runner's allowed inference host is reachable,
proves that eight other destination classes are not, appends the result to the
audit chain only when every part passed, and deletes the sandbox.

Eight decisions here are load-bearing.

**The probe runs under EVERY governed binary's clause.** Every policy in
``docker/openshell/policy-*.yaml`` grants egress per image path, and the path
that owns a dispatch's API socket is the runner's native binary — which cannot
run a probe program. The probe is a Node program, so a single run under the
shipped policy exercised only the ``/usr/bin/node`` clause, and a policy whose
runner clause alone was widened still certified (#2624). So the policy is split
into its per-binary clauses (:func:`governed_binary_clauses`) and the probe runs
once per clause, each time in a sandbox whose policy grants that clause — and
nothing else — to :data:`PROBE_INTERPRETER`. The certification carries one
matrix per governed binary and passes only when every one of them did.

**The bytes hashed are the bytes run.** The policy is read once
(:class:`PinnedPolicy`); its digest is of that read, and every probe sandbox is
created from a private copy of those bytes (:func:`materialized_policy`), never
from the packaged path a second read could find swapped.

**The gateway is identified by what it says about itself.** The CLI version
on this host is not the gateway that enforced the probe, so the certification
also binds :func:`openshell_gateway_identity` — and admission recomputes it
against the gateway a dispatch would launch on.

**The launcher is read inside the image.** The probe hashes
:data:`LAUNCHER_PATH` from inside the sandbox, so the launcher a certification
binds is the one the image would run, not this host's package copy — and a
certification whose image carries some other launcher is refused.

**A probe sandbox that survived is a failed certification.** Nothing is
recorded when any probe sandbox could not be deleted, and the command exits
non-zero: a proof whose own cleanup left an un-owned container behind is not a
clean run.

**Each protocol proves itself.** The positive control is TCP over IPv4, so it
says nothing about a sandbox with no UDP path or no IPv6 route — and both of
those deny their destination class whatever the policy enforces. The ``udp_dns``
and ``ipv6_external`` legs therefore each carry their own same-protocol control
(the sandbox's configured resolver over UDP, and the allowed inference host
over IPv6), and a leg whose control did not hold fails the certification
instead of passing on an absent protocol. This is deliberately the fail-closed
direction: a boundary that simply has no IPv6 is refused rather than certified
on an untested leg, and ``kew sandbox certify`` prints the control's own detail
so the operator sees which one to fix.

**A refused connection counts as a leak.** The classification table below maps
what Node reported to one of three statuses, and it errs toward failing the
certification: ``ECONNREFUSED`` / ``ECONNRESET`` mean a packet reached a host
that answered, which is not a closed boundary even though nothing useful was
transferred. Only "the packet went nowhere" spellings — unreachable, denied by
a local rule, no DNS answer, or silence until the deadline — count as denials.

**Nothing becomes a denial by default.** A probe that could not be attempted
reports ``unavailable``, and :func:`~kew.egress_certification.build_packet_evidence`
refuses a matrix with a missing entry, so an incomplete probe fails the
certification instead of shortening it.

**The sandbox is deleted on every path.** A probe sandbox that outlived its
probe would be an un-owned container holding the gateway's resources, so the
delete runs in a ``finally`` and its own failure is reported rather than
swallowed — including when the probe itself already failed.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import secrets
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from kew import work_dirs
from kew.config import DispatchConfig
from kew.credential_surface import exception_text
from kew.egress_certification import (
    BINARY_PATH_RE,
    CERTIFICATION_SCHEMA_VERSION,
    OPENSHELL_PACKET_CONTROLS,
    OPENSHELL_PACKET_DENIALS,
    EgressPacketEvidence,
    RuntimeEgressCertification,
    build_packet_evidence,
    bytes_digest,
    file_digest,
)
from kew.openshell import (
    OpenShellClient,
    OpenShellError,
    OpenShellNotFound,
)
from kew.readiness import PacketProbeStatus
from kew.repo_host import repo_host
from kew.runtime_names import OPENSHELL_RUNTIME

#: The inference hosts each runner's OpenShell policy allows, in canonical
#: order, with the FIRST being the one the positive control dials. Most runners
#: have exactly one; ``codex`` has three, because ChatGPT-mode Codex is not a
#: single-endpoint client (see ``docker/openshell/policy-codex.yaml``). The
#: whole set is carried, not just the control's host, so the probe's denied
#: targets can be checked disjoint from every host any policy permits.
#:
#: Duplicated from the policy files on purpose: production code must not parse
#: YAML (PyYAML is not a kew dependency — see ``pyproject.toml``), so the
#: coupling is held by ``tests/test_openshell_certification.py``, which globs
#: EVERY shipped ``docker/openshell/policy-*.yaml`` and fails both when a
#: policy's endpoint hosts disagree with this map and when a policy ships with
#: no entry here at all. The second half is why ``codex`` is here: its policy
#: shipped at #2371 and this map did not follow, so a codex OpenShell boundary
#: could not be certified and the refusal said no policy existed.
OPENSHELL_INFERENCE_HOSTS: Final[Mapping[str, tuple[str, ...]]] = {
    "claude-code": ("api.anthropic.com",),
    "codex": ("api.openai.com", "auth.openai.com", "chatgpt.com"),
    "opencode": ("openrouter.ai",),
}

#: The launch a policy governs. ``[sandbox] runtime`` names ONE container
#: boundary per runtime, but not every launch into it needs the same egress,
#: and the adversarial reviewer is why (epic node N12): its prompt requires it
#: to execute the reviewed PR's tests, so a boundary proven for a dispatch says
#: nothing about it. Each flow therefore has its own policy file, its own
#: digest, and — where it can be probed at all — its own certification.
DISPATCH_FLOW: Final = "dispatch"
REVIEW_FLOW: Final = "review"
PROVISION_FLOW: Final = "review-provision"

#: Every endpoint host each packaged policy names, per flow and runner. Read
#: the duplication warning above: production code must not parse YAML, so this
#: map is the reviewable constant and ``tests/test_openshell_policy.py`` holds
#: it equal to the shipped files — in both directions, so a policy added with
#: no entry here fails rather than shipping an unverifiable boundary.
#:
#: The dispatch flow's entry IS :data:`OPENSHELL_INFERENCE_HOSTS`, not a copy:
#: a second spelling of the dispatch hosts is a second answer waiting to
#: disagree with the one ``openshell_inference_host`` already serves.
OPENSHELL_POLICY_HOSTS: Final[Mapping[str, Mapping[str, tuple[str, ...]]]] = {
    DISPATCH_FLOW: OPENSHELL_INFERENCE_HOSTS,
    # Narrower than the dispatch policy by construction, and never wider: the
    # reviewer needs the model API and nothing else once its environment is
    # provisioned.
    REVIEW_FLOW: {"claude-code": ("api.anthropic.com",)},
    # The index, and no model host at all. Nothing in the provisioning sandbox
    # talks to an inference API — it has no credential to talk with.
    PROVISION_FLOW: {
        "claude-code": ("files.pythonhosted.org", "pypi.org"),
    },
}

#: The flows ``kew sandbox certify`` can prove. The certification probe is a
#: Node program run inside the boundary, so a flow whose policy deliberately
#: leaves node ungoverned cannot be probed: the control would fail for a policy
#: working exactly as written. :data:`PROVISION_FLOW` is that flow, and it is
#: refused by name rather than probed — "kew could not check" is never a pass,
#: and neither is a check that cannot mean anything.
CERTIFIABLE_FLOWS: Final[tuple[str, ...]] = (DISPATCH_FLOW, REVIEW_FLOW)

#: The image store the OpenShell gateway's driver shares with this host. The
#: 2026-10-03 spike ran the Docker driver (``openshell status`` reports
#: ``driver: docker``), so the runner image a sandbox launches is the one
#: ``docker image inspect`` resolves. A gateway on another driver cannot be
#: certified from here, and the resolution failure says so rather than
#: certifying an image ID the gateway never saw.
IMAGE_DRIVER_RUNTIME: Final = "docker"

#: The interpreter the probe runs as, by absolute image path. Each probe
#: sandbox's policy grants exactly one governed binary's clause to this path
#: (:func:`binary_probe_policy`), so the probe observes that clause's rules.
PROBE_INTERPRETER: Final = "/usr/bin/node"

#: The in-sandbox launcher every agent image installs (node N3). It is the
#: image ENTRYPOINT too, but ``openshell sandbox exec`` replaces an entrypoint
#: rather than running it, so the host names the absolute path itself. Defined
#: here, not in :mod:`kew.openshell_launch`, because the probe hashes it from
#: inside the image and the launch execs it: one path, one definition.
LAUNCHER_PATH: Final = "/usr/local/bin/kew-openshell-launch"

#: Port probed on the sandbox's own default gateway. OpenShell's gateway listens
#: there in the spike's configuration (``http://127.0.0.1:8080``), so this is
#: not an arbitrary port: reaching it from inside a sandbox means the contained
#: agent can speak to the control plane that is supposed to be governing it.
DEFAULT_GATEWAY_PROBE_PORT: Final = 8080

#: How long each individual probe waits before calling the packet dropped.
PROBE_DEADLINE_SECONDS: Final = 8.0
#: Bound on the whole exec. One deadline plus room for Node to start and for
#: the gateway to stream the output back.
EXEC_TIMEOUT_SECONDS: Final = 60.0

#: Marker the probe prints its JSON result line behind, so the result survives
#: anything else the image or the gateway writes to the merged stream.
RESULT_MARKER: Final = "KEW-EGRESS-PROBE "

#: Wire format between the inline probe script and :func:`parse_probe_output`.
#: Both ship in this module, so the two can never be a version apart — the
#: number is here so a result from some OTHER program that happened to print
#: the marker is refused rather than read. ``2`` added the per-leg
#: same-protocol controls; ``3`` the in-image launcher digest.
#:
#: The script does NOT repeat the number: it echoes back the ``schema`` it is
#: handed in :func:`probe_parameters`. Written as a literal on both sides, this
#: constant could be bumped with the script left behind, and every test in the
#: suite stayed green while every real ``kew sandbox certify`` run refused its
#: own probe as "unknown schema" (#2389 review) — the suite cannot execute the
#: script, so one definition is the only coupling that holds.
PROBE_RESULT_SCHEMA: Final = 3

_SANDBOX_NAME_PREFIX: Final = "kew-egress-certify-"

#: The policy key whose list this module splits per binary, and the one line
#: shape a clause may start with. Production code does not parse YAML (see
#: :data:`OPENSHELL_INFERENCE_HOSTS`), so the split accepts the exact layout
#: every shipped policy uses and refuses anything else rather than guessing.
_BINARIES_LINE: Final = "binaries:"
_CLAUSE_PREFIX: Final = "  - path: "
_CLAUSE_BODY_INDENT: Final = "    "
_LAUNCHER_DIGEST_RE: Final = re.compile(r"^[0-9a-f]{64}$")

_logger = logging.getLogger(__name__)

_PROBE_SCRIPT: Final = r"""
'use strict';
// kew OpenShell egress probe. Runs as /usr/bin/node, which every kew OpenShell
// policy governs with the same inference endpoints the runner itself gets.
const net = require('net');
const tls = require('tls');
const dgram = require('dgram');
const dns = require('dns');
const fs = require('fs');
const crypto = require('crypto');

const params = JSON.parse(process.argv[1]);
const DEADLINE = params.deadline_ms;
const MARKER = params.marker;
// Echoed back, never written here: PROBE_RESULT_SCHEMA is the one definition.
const SCHEMA = params.schema;
const REACHABLE = 'reachable';
const DENIED = 'denied';
const UNAVAILABLE = 'unavailable';

// The packet went nowhere: no route, a local rule refused it, or no DNS answer.
const BLOCKED = new Set([
  'EACCES', 'EPERM', 'ENETUNREACH', 'EHOSTUNREACH', 'ENETDOWN', 'EHOSTDOWN',
  'ENOTFOUND', 'EAI_AGAIN', 'EAI_NONAME', 'EADDRNOTAVAIL', 'EAFNOSUPPORT',
  'ETIMEDOUT', 'ENOENT',
]);
// Something answered. Not a closed boundary, however little was transferred.
const ANSWERED = new Set(['ECONNREFUSED', 'ECONNRESET']);

function classify(error) {
  const code = String((error && error.code) || '');
  if (ANSWERED.has(code)) return { status: REACHABLE, detail: code };
  if (BLOCKED.has(code)) return { status: DENIED, detail: code };
  // A TLS failure is still a completed round trip to the destination.
  if (code.startsWith('ERR_TLS') || code.indexOf('CERT') !== -1) {
    return { status: REACHABLE, detail: code };
  }
  return { status: UNAVAILABLE, detail: code || String(error) };
}

function settle(resolve, cleanup) {
  let done = false;
  return function (status, detail) {
    if (done) return;
    done = true;
    try { cleanup(); } catch (error) { /* already closing */ }
    resolve({ status: status, detail: detail });
  };
}

function tcpProbe(host, port, secure, family) {
  return new Promise(function (resolve) {
    let socket;
    const finish = settle(resolve, function () { socket.destroy(); });
    try {
      socket = secure
        ? tls.connect({
            host: host, port: port, servername: host,
            timeout: DEADLINE, family: family,
          })
        : net.connect({ host: host, port: port, timeout: DEADLINE, family: family });
    } catch (error) {
      const verdict = classify(error);
      resolve(verdict);
      return;
    }
    socket.setTimeout(DEADLINE);
    socket.on(secure ? 'secureConnect' : 'connect', function () {
      finish(REACHABLE, 'connected');
    });
    socket.on('timeout', function () { finish(DENIED, 'timeout'); });
    socket.on('error', function (error) {
      const verdict = classify(error);
      finish(verdict.status, verdict.detail);
    });
  });
}

function udpProbe(host, port) {
  return new Promise(function (resolve) {
    const socket = dgram.createSocket(host.indexOf(':') === -1 ? 'udp4' : 'udp6');
    const finish = settle(resolve, function () { socket.close(); });
    const timer = setTimeout(function () { finish(DENIED, 'timeout'); }, DEADLINE);
    timer.unref();
    socket.on('message', function () { finish(REACHABLE, 'answered'); });
    socket.on('error', function (error) {
      const verdict = classify(error);
      finish(verdict.status, verdict.detail);
    });
    // A minimal DNS A query for "." — enough to draw a reply from a resolver.
    const query = Buffer.from([
      0x6b, 0x65, 0x01, 0x00, 0x00, 0x01, 0x00, 0x00,
      0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x01,
    ]);
    socket.send(query, port, host, function (error) {
      if (error) {
        const verdict = classify(error);
        finish(verdict.status, verdict.detail);
      }
    });
  });
}

function nameProbe(name) {
  return new Promise(function (resolve) {
    const timer = setTimeout(function () {
      resolve({ status: DENIED, detail: 'timeout' });
    }, DEADLINE);
    timer.unref();
    dns.lookup(name, { all: true }, function (error, addresses) {
      clearTimeout(timer);
      if (error) {
        const verdict = classify(error);
        resolve(verdict);
        return;
      }
      if (!addresses || addresses.length === 0) {
        resolve({ status: DENIED, detail: 'no address' });
        return;
      }
      resolve({ status: REACHABLE, detail: addresses[0].address });
    });
  });
}

function defaultGateway() {
  // /proc/net/route: a destination of 00000000 is the default route, and the
  // gateway column is a little-endian hex IPv4 address.
  const lines = fs.readFileSync('/proc/net/route', 'utf8').split('\n');
  for (let index = 1; index < lines.length; index += 1) {
    const columns = lines[index].trim().split(/\s+/);
    if (columns.length < 3 || columns[1] !== '00000000') continue;
    const raw = columns[2];
    if (!/^[0-9A-Fa-f]{8}$/.test(raw)) continue;
    // Little-endian: the last hex byte is the first octet of the address.
    const octets = [];
    for (let byte = 3; byte >= 0; byte -= 1) {
      octets.push(parseInt(raw.substr(byte * 2, 2), 16));
    }
    return octets.join('.');
  }
  return null;
}

function resolverAddress() {
  // dns.getServers() is the sandbox's own /etc/resolv.conf, which is the one
  // UDP peer a boundary must permit for the agent to resolve its allowed host
  // at all. Entries are bare addresses or "[::1]:5353".
  const servers = dns.getServers();
  for (const server of servers) {
    const bracketed = /^\[(.+)\](?::\d+)?$/.exec(server);
    if (bracketed) return bracketed[1];
    const plain = /^([0-9][0-9.]*)(?::\d+)?$/.exec(server);
    if (plain) return plain[1];
  }
  return null;
}

async function udpControl() {
  // The same-protocol control for `udp_dns`: without it, a sandbox with no UDP
  // path whatsoever records "denied" for UDP to a public resolver and the leg
  // certifies nothing. Reaching the CONFIGURED resolver over UDP proves
  // datagrams flow in this namespace, so the public resolver's silence is the
  // boundary dropping them.
  const address = resolverAddress();
  if (!address) {
    return { status: UNAVAILABLE, detail: 'no resolver in /etc/resolv.conf' };
  }
  const verdict = await udpProbe(address, 53);
  return { status: verdict.status, detail: address + ':53 ' + verdict.detail };
}

async function ipv6Control(host) {
  // The same-protocol control for `ipv6_external`: the allowed inference host,
  // over IPv6 only. A sandbox with no IPv6 route denies every IPv6 destination
  // whatever the policy says, so without this the leg reads as closed on an
  // IPv4-only boundary kew never tested for IPv6 enforcement.
  const verdict = await tcpProbe(host, 443, true, 6);
  return { status: verdict.status, detail: '[ipv6] ' + verdict.detail };
}

async function gatewayProbe(port) {
  let address = null;
  try {
    address = defaultGateway();
  } catch (error) {
    return { status: UNAVAILABLE, detail: 'route table unreadable' };
  }
  if (!address) return { status: UNAVAILABLE, detail: 'no default route' };
  const verdict = await tcpProbe(address, port, false);
  const where = address + ':' + port + ' ';
  return { status: verdict.status, detail: where + verdict.detail };
}

function launcherDigest(path) {
  // The launcher THIS image would run, read from inside it. Null when it
  // cannot be read: the host refuses a result without one.
  try {
    return crypto.createHash('sha256').update(fs.readFileSync(path)).digest('hex');
  } catch (error) {
    return null;
  }
}

async function main() {
  const control = await tcpProbe(params.inference_host, 443, true);
  const targets = {};
  const controls = {};
  const details = { positive_control: control.detail };
  // One same-protocol control per leg whose protocol the shared TCP/IPv4
  // control cannot speak for. Keyed by the leg they govern, so the host side
  // cannot attach one to the wrong target.
  const sameProtocol = [
    ['udp_dns', udpControl()],
    ['ipv6_external', ipv6Control(params.inference_host)],
  ];
  const probes = [
    ['external_host', tcpProbe(params.external_host, 443, true)],
    ['public_ip', tcpProbe(params.public_ip, 443, false)],
    ['udp_dns', udpProbe(params.public_ip, 53)],
    ['ipv6_external', tcpProbe(params.ipv6_host, 443, false, 6)],
    ['link_local_metadata', tcpProbe(params.metadata_ip, 80, false)],
    ['host_gateway', gatewayProbe(params.gateway_port)],
    ['github', tcpProbe(params.github_host, 443, true)],
    ['unresolvable_name', nameProbe(params.unresolvable_name)],
  ];
  for (const [name, pending] of sameProtocol) {
    const verdict = await pending;
    controls[name] = verdict.status;
    details[name + '_control'] = verdict.detail;
  }
  for (const [name, pending] of probes) {
    const verdict = await pending;
    targets[name] = verdict.status;
    details[name] = verdict.detail;
  }
  process.stdout.write(MARKER + JSON.stringify({
    schema: SCHEMA,
    positive_control: control.status,
    controls: controls,
    targets: targets,
    details: details,
    launcher_sha256: launcherDigest(params.launcher_path),
  }) + '\n');
}

main().then(function () { process.exit(0); }, function (error) {
  process.stdout.write(MARKER + JSON.stringify({
    schema: SCHEMA,
    error: String((error && error.message) || error),
  }) + '\n');
  process.exit(1);
});
"""


class EgressProbeError(RuntimeError):
    """The probe could not be run, or said something kew cannot read."""


@dataclass(frozen=True)
class CertificationResult:
    """What one ``kew sandbox certify`` run observed and what it recorded.

    ``event_id`` is ``None`` for every non-passing verdict, and that is the
    whole contract between this module and the audit chain: a failed boundary
    leaves no evidence an admission check could later read as a pass.
    """

    certification: RuntimeEgressCertification
    event_id: int | None
    details: Mapping[str, str]
    sandbox_names: tuple[str, ...]
    cleanup_error: str | None

    @property
    def passed(self) -> bool:
        """The boundary held AND every probe sandbox was deleted.

        A surviving probe sandbox is a container kew created and no longer
        owns, so a run that left one is not a clean certification and records
        nothing, whatever its matrix said.
        """
        return self.certification.verdict == "passed" and self.cleanup_error is None


def packaged_openshell_policy_runners(flow: str = DISPATCH_FLOW) -> tuple[str, ...]:
    """The runners this build ships an OpenShell policy for, in name order.

    Read off :data:`OPENSHELL_POLICY_HOSTS`, whose entries
    ``test_every_shipped_policy_has_an_inference_host_entry`` holds equal to the
    packaged ``policy-*.yaml`` set. So the answer costs no filesystem read and
    cannot drift from the shipped files — which is what admission needs, because
    it asks this question before it has resolved an image, a gateway or a
    ledger: ``pi`` and ``hermes`` have to be refused for the reason that is
    actually true of them, not for a boundary left unproven.

    ``flow`` is asked per launch rather than per runtime: ``claude-code`` is the
    only runner that can review (``kew.review_stream.STREAM_VERDICT_RUNNERS``),
    so the review flows ship one policy and a reviewer pinned to any other
    runner has to be refused for the reason that is true of it.
    """
    return tuple(sorted(policy_flow_hosts(flow)))


def policy_flow_hosts(flow: str) -> Mapping[str, tuple[str, ...]]:
    """The per-runner endpoint hosts of ``flow``, or a raise naming the flows.

    One lookup site, so a flow name that is a typo cannot resolve to an empty
    mapping and read as "this build ships no policy for any runner".
    """
    try:
        return OPENSHELL_POLICY_HOSTS[flow]
    except KeyError:
        raise EgressProbeError(
            f"no OpenShell policy flow named {flow!r}; this build governs "
            f"{', '.join(sorted(OPENSHELL_POLICY_HOSTS))}"
        ) from None


def policy_file_stem(runner: str, flow: str = DISPATCH_FLOW) -> str:
    """The ``policy-<stem>.yaml`` name governing ``runner`` on ``flow``.

    The dispatch flow keeps the bare runner name the shipped files have always
    used — renaming it would invalidate every certification on every operator's
    chain, since the digest is of the file and the match is on the digest.
    """
    policy_flow_hosts(flow)
    return runner if flow == DISPATCH_FLOW else f"{flow}-{runner}"


def openshell_policy_path(runner: str, *, flow: str = DISPATCH_FLOW) -> Path:
    """The packaged OpenShell policy for ``runner`` on ``flow``, or a raise.

    Resolved through the packaged image context, which validates the shipped
    file-and-mode manifest first — so a policy that was edited, replaced or
    removed in an installed wheel fails here rather than being certified.
    """
    from kew.sandbox import packaged_agent_image_context

    name = f"policy-{policy_file_stem(runner, flow)}.yaml"
    path = packaged_agent_image_context() / "docker" / "openshell" / name
    if not path.is_file():
        raise EgressProbeError(
            f"no packaged OpenShell {flow} policy for runner {runner!r} "
            f"(expected {path.name})"
        )
    return path


@dataclass(frozen=True)
class PinnedPolicy:
    """One read of a packaged policy: the bytes hashed are the bytes used.

    Hashing the path and later handing the same path to ``sandbox create``
    reads the file twice, and a file swapped between the reads is launched (or
    certified) under bytes no digest ever matched (#2624). Every consumer takes
    :attr:`data` instead — through :func:`materialized_policy` when a gateway
    call needs a path.
    """

    path: Path
    data: bytes

    @property
    def sha256(self) -> str:
        return bytes_digest(self.data)


def pin_openshell_policy(runner: str, *, flow: str = DISPATCH_FLOW) -> PinnedPolicy:
    """Resolve and read ``runner``'s ``flow`` policy exactly once."""
    path = openshell_policy_path(runner, flow=flow)
    return PinnedPolicy(path=path, data=path.read_bytes())


@contextmanager
def materialized_policy(data: bytes) -> Iterator[Path]:
    """A private file holding exactly ``data``, removed on exit.

    Created exclusively inside a fresh ``0700`` directory, so nothing but this
    process could have written the bytes the gateway is handed.
    """
    with work_dirs.work_tempdir("kew-openshell-policy-") as directory:
        path = Path(directory) / "policy.yaml"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        yield path


def governed_binary_clauses(data: bytes) -> tuple[str, tuple[tuple[str, str], ...]]:
    """Split a policy into its header and its per-binary clauses.

    Returns everything up to and including the ``binaries:`` line, then one
    ``(path, body)`` per clause in the policy's order, ``body`` being the
    clause's lines after its ``- path:`` line. Only the layout every shipped
    policy uses is accepted: a top-level key after the list, a quoted or
    relative path, a repeated path or an empty list raises, because a clause
    this split misread is a clause the certification would not have probed.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EgressProbeError("the OpenShell policy is not UTF-8") from exc
    lines = text.splitlines(keepends=True)
    starts = [
        index
        for index, line in enumerate(lines)
        if line.rstrip("\r\n") == _BINARIES_LINE
    ]
    if len(starts) != 1:
        raise EgressProbeError(
            f"the OpenShell policy has {len(starts)} {_BINARIES_LINE!r} lines, "
            "expected one"
        )
    clauses: list[tuple[str, list[str]]] = []
    for line in lines[starts[0] + 1 :]:
        _split_clause_line(line, clauses)
    paths = [path for path, _ in clauses]
    if not paths:
        raise EgressProbeError("the OpenShell policy governs no binary")
    if len(set(paths)) != len(paths):
        raise EgressProbeError("the OpenShell policy repeats a governed binary")
    header = "".join(lines[: starts[0] + 1])
    return header, tuple((path, "".join(body)) for path, body in clauses)


def _split_clause_line(line: str, clauses: list[tuple[str, list[str]]]) -> None:
    """File one line of the ``binaries:`` list, or refuse a shape it is not."""
    bare = line.rstrip("\r\n")
    if bare.startswith(_CLAUSE_PREFIX):
        path = bare[len(_CLAUSE_PREFIX) :].strip()
        if BINARY_PATH_RE.match(path) is None:
            raise EgressProbeError(
                f"the OpenShell policy governs a path this build cannot probe: {path!r}"
            )
        clauses.append((path, []))
    elif not bare.strip() or bare.lstrip().startswith("#"):
        if clauses:
            clauses[-1][1].append(line)
    elif bare.startswith(_CLAUSE_BODY_INDENT) and clauses:
        clauses[-1][1].append(line)
    else:
        raise EgressProbeError(
            "the OpenShell policy's binaries list has a line this build cannot "
            f"attribute to a clause: {bare.strip()[:60]!r}"
        )


def binary_probe_policy(header: str, body: str) -> bytes:
    """A policy granting one governed binary's clause to the probe, and no other.

    The probe cannot run AS a native runner binary, so it runs as
    :data:`PROBE_INTERPRETER` under that binary's rules instead: the header and
    the clause body are the shipped bytes verbatim, and only the path the
    clause is keyed on changes.
    """
    return (header + f"{_CLAUSE_PREFIX}{PROBE_INTERPRETER}\n" + body).encode("utf-8")


def openshell_gateway_identity(client: OpenShellClient) -> str:
    """A digest of which gateway ``client`` reaches, as that gateway states it.

    The CLI version on this host says nothing about which gateway enforced a
    probe: certify against one, launch against another, and the version alone
    matches (#2624). So the endpoint kew dials is bound together with the
    running gateway's own self-report — the address it states, its build
    version and the supervisor and sandbox images it launches from.
    """
    report = client.report()
    stated = report.raw.get("gateway")
    facts = {
        "endpoint": client.gateway.endpoint,
        "stated_endpoint": stated if isinstance(stated, str) else "",
        "version": report.version,
        "supervisor_image": report.supervisor_image,
        "sandbox_image": report.sandbox_image,
    }
    return bytes_digest(
        json.dumps(facts, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def openshell_supervisor_digest() -> str:
    """SHA-256 of the launcher this kew ships to agent images.

    ``docker/kew-openshell-launch`` is the program that resolves and execs the
    runner inside the sandbox, so it decides which governed binary actually
    runs under the policy. A certification binds the copy read INSIDE the
    image (:func:`probe_launcher_digest`) and is refused unless it equals this
    one — so admission, which cannot read inside the image, matches the image
    ID together with this digest and is matching the launcher that ran.
    """
    from kew.sandbox import packaged_agent_image_context

    return file_digest(
        packaged_agent_image_context() / "docker" / "kew-openshell-launch"
    )


def openshell_inference_host(runner: str, *, flow: str = DISPATCH_FLOW) -> str:
    """The host the runner's positive control dials, or a raise naming it.

    The first of the runner's allowed endpoints on ``flow``. For a
    multi-endpoint policy the others are allowed too; the control needs one that
    the policy permits and that answers a TLS handshake, which is the inference
    API itself.
    """
    hosts = policy_flow_hosts(flow).get(runner)
    if not hosts:
        supported = ", ".join(packaged_openshell_policy_runners(flow))
        raise EgressProbeError(
            f"runner {runner!r} has no OpenShell {flow} policy to certify; "
            f"this build certifies {supported}"
        )
    return hosts[0]


def gateway_probe_port(endpoint: str) -> int:
    """The port the configured gateway endpoint names, or OpenShell's default.

    The endpoint is empty when neither ``OPENSHELL_GATEWAY`` nor
    ``[sandbox.openshell] gateway`` supplied one and the CLI used its own
    default; the probe still needs a concrete port, so it falls back to the one
    the gateway listens on rather than skipping the probe.
    """
    _, _, tail = endpoint.rpartition(":")
    port = tail.partition("/")[0]
    if port.isdigit() and 0 < int(port) < 65536:
        return int(port)
    return DEFAULT_GATEWAY_PROBE_PORT


def probe_parameters(
    *, inference_host: str, gateway_port: int, forge_host: str
) -> dict[str, object]:
    """The destinations the probe dials, as the one place they are written.

    Each is reserved, documentation-assigned or deliberately well known, and
    each is genuinely routable from an unrestricted host — a reserved-but-
    unroutable address would time out on an open boundary and certify it as
    closed, which is the exact false pass this gate exists to prevent. The one
    exception is ``unresolvable_name``, whose ``.invalid`` TLD must never
    resolve anywhere: it probes the resolver, not a route.

    ``forge_host`` is the project's own Git forge, resolved from its origin
    remote: an Enterprise install must prove the sandbox cannot reach the forge
    IT pushes to, which is not the one a literal would name.
    """
    return {
        "schema": PROBE_RESULT_SCHEMA,
        "deadline_ms": int(PROBE_DEADLINE_SECONDS * 1000),
        "marker": RESULT_MARKER,
        "inference_host": inference_host,
        "external_host": "example.com",
        "public_ip": "1.1.1.1",
        "ipv6_host": "2606:4700:4700::1111",
        "metadata_ip": "169.254.169.254",
        "github_host": forge_host,
        "unresolvable_name": "kew-egress-certification.invalid",
        "gateway_port": gateway_port,
        "launcher_path": LAUNCHER_PATH,
    }


def _probe_result_payload(output: str) -> dict[str, object]:
    """The probe's one marked result object, or a raise naming what arrived.

    The marker exists because the exec stream is stdout and stderr merged: the
    image's entrypoint, Node and the gateway can all write to it, and a parser
    that took the last line would read whichever of them spoke last.
    """
    lines = [
        line[len(RESULT_MARKER) :]
        for line in output.splitlines()
        if line.startswith(RESULT_MARKER)
    ]
    if len(lines) != 1:
        raise EgressProbeError(
            f"the egress probe printed {len(lines)} result lines, expected one"
        )
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise EgressProbeError(
            f"the egress probe result is not JSON ({exc.msg})"
        ) from exc
    if not isinstance(payload, dict) or payload.get("schema") != PROBE_RESULT_SCHEMA:
        raise EgressProbeError("the egress probe result has an unknown schema")
    if "error" in payload:
        raise EgressProbeError(f"the egress probe failed: {payload['error']}")
    return payload


def parse_probe_output(
    output: str,
) -> tuple[PacketProbeStatus, dict[str, str], dict[str, str], dict[str, str]]:
    """Read the probe's verdicts, refusing a status this build does not know.

    Returns the shared positive control, the per-leg same-protocol controls,
    the denial observations and the human-readable details. The controls are
    required to be exactly the runtime's contract: a probe that reported none
    would otherwise fall back to the shared control for a UDP and an IPv6 leg
    it cannot speak for, which is the false pass they exist to stop.
    """
    payload = _probe_result_payload(output)
    targets = payload.get("targets")
    details = payload.get("details")
    controls = payload.get("controls")
    if not isinstance(targets, dict) or not isinstance(details, dict):
        raise EgressProbeError("the egress probe result is malformed")
    if not isinstance(controls, dict) or set(controls) != set(
        OPENSHELL_PACKET_CONTROLS
    ):
        expected = ", ".join(sorted(OPENSHELL_PACKET_CONTROLS))
        raise EgressProbeError(
            f"the egress probe reported no same-protocol control for {expected}"
        )
    try:
        control = PacketProbeStatus(str(payload.get("positive_control")))
        observations = {
            str(name): PacketProbeStatus(str(value)).value
            for name, value in targets.items()
        }
        observed_controls = {
            str(name): PacketProbeStatus(str(value)).value
            for name, value in controls.items()
        }
    except ValueError as exc:
        raise EgressProbeError(f"the egress probe reported {exc}") from exc
    return (
        control,
        observations,
        observed_controls,
        {str(name): str(value) for name, value in details.items()},
    )


def probe_launcher_digest(output: str) -> str:
    """The launcher digest the probe read inside the image, or a raise."""
    digest = _probe_result_payload(output).get("launcher_sha256")
    if not isinstance(digest, str) or _LAUNCHER_DIGEST_RE.match(digest) is None:
        raise EgressProbeError(
            f"the egress probe could not read the image's launcher at {LAUNCHER_PATH}"
        )
    return digest


def _probe_sandbox_name() -> str:
    return f"{_SANDBOX_NAME_PREFIX}{secrets.token_hex(6)}"


def _run_probe(
    client: OpenShellClient,
    *,
    name: str,
    image_id: str,
    policy: bytes,
    runner: str,
    flow: str,
    parameters: Mapping[str, object],
) -> tuple[EgressPacketEvidence, dict[str, str], str]:
    """Create the probe sandbox, run the probe in it, and read its verdict.

    Returns the matrix, the details and the launcher digest read in the image.
    """
    with materialized_policy(policy) as policy_path:
        client.sandbox_create(
            name,
            image=image_id,
            policy_path=policy_path,
            labels={"kew.certify": "egress", "kew.runner": runner, "kew.flow": flow},
        )
    handle = client.sandbox_exec(
        name,
        workdir="/",
        argv=[
            PROBE_INTERPRETER,
            "-e",
            _PROBE_SCRIPT,
            json.dumps(parameters, sort_keys=True),
        ],
    )
    handle.wait(EXEC_TIMEOUT_SECONDS)
    output = "".join(handle.drained)
    control, raw, raw_controls, details = parse_probe_output(output)
    packet = build_packet_evidence(
        positive_control=control,
        observations={name: PacketProbeStatus(value) for name, value in raw.items()},
        required=OPENSHELL_PACKET_DENIALS,
        controls={
            name: PacketProbeStatus(value) for name, value in raw_controls.items()
        },
    )
    return packet, details, probe_launcher_digest(output)


def require_certifiable_flow(flow: str) -> str:
    """Return ``flow`` once a probe run under its policy could mean something.

    :data:`PROVISION_FLOW` is refused here rather than probed. Its policy
    governs ``uv`` alone — the whole point, since every build backend uv
    executes is the reviewed PR's own code — and the probe is a Node program,
    so node would be denied every destination and the positive control would
    fail for a policy that is working exactly as written. A certification run
    that cannot pass is worse than an absent one: it reads as a leaking
    boundary.
    """
    if flow in CERTIFIABLE_FLOWS:
        return flow
    policy_flow_hosts(flow)
    raise EgressProbeError(
        f"the OpenShell {flow} boundary cannot be certified: its policy "
        "governs no interpreter the probe can run inside, so a probe would "
        f"report a failed control for a working policy. Certifiable flows: "
        f"{', '.join(CERTIFIABLE_FLOWS)}."
    )


def certify_openshell_egress(
    config: DispatchConfig,
    *,
    runner: str,
    store: object,
    repo: str | None = None,
    flow: str = DISPATCH_FLOW,
) -> CertificationResult:
    """Prove, record and report one OpenShell boundary's closed egress.

    The audit append happens here rather than in the CLI on purpose: the whole
    point of this node is that the certification writer has a production
    caller, so the command that proves a boundary is the same code path that
    records the proof.

    ``flow`` names WHICH boundary on this runtime is being proven (epic node
    N12). Every bound fact but the image and the launcher differs per flow,
    because the policy does — so a dispatch certification and a review one are
    two separate proofs on the chain and neither can stand in for the other.
    """
    from kew.audit import (
        append_runtime_egress_certification,
        egress_certification_actor,
    )
    from kew.sandbox import require_image_id, resolve_runner_image

    require_certifiable_flow(flow)
    inference_host = openshell_inference_host(runner, flow=flow)
    policy = pin_openshell_policy(runner, flow=flow)
    header, clauses = governed_binary_clauses(policy.data)
    shipped_launcher = openshell_supervisor_digest()
    image = resolve_runner_image(config.sandbox, runner)
    image_id = require_image_id(
        IMAGE_DRIVER_RUNTIME,
        image,
        runner=runner,
        refusal="the OpenShell egress certification is refused before any probe",
    )
    client = OpenShellClient.from_settings(config.sandbox.openshell)
    gateway_version = client.check_version()
    gateway_identity = openshell_gateway_identity(client)
    parameters = probe_parameters(
        inference_host=inference_host,
        gateway_port=gateway_probe_port(client.gateway.endpoint),
        forge_host=repo_host(),
    )
    packets: list[tuple[str, EgressPacketEvidence]] = []
    details: dict[str, str] = {}
    names: list[str] = []
    cleanup_errors: list[str] = []
    launchers: set[str] = set()
    for binary, body in clauses:
        name = _probe_sandbox_name()
        names.append(name)
        try:
            packet, observed, launcher = _run_probe(
                client,
                name=name,
                image_id=image_id,
                policy=binary_probe_policy(header, body),
                runner=runner,
                flow=flow,
                parameters=parameters,
            )
        finally:
            cleanup_error = _delete_probe_sandbox(client, name)
            if cleanup_error is not None:
                cleanup_errors.append(f"{name}: {cleanup_error}")
        packets.append((binary, packet))
        launchers.add(launcher)
        details.update({f"{binary} {key}": value for key, value in observed.items()})
    certification = RuntimeEgressCertification(
        schema_version=CERTIFICATION_SCHEMA_VERSION,
        runtime=OPENSHELL_RUNTIME,
        image_id=image_id,
        policy_sha256=policy.sha256,
        gateway_version=gateway_version,
        gateway_identity=gateway_identity,
        supervisor_digest=_require_shipped_launcher(launchers, shipped_launcher),
        packets=tuple(packets),
    )
    result = CertificationResult(
        certification=certification,
        event_id=None,
        details=details,
        sandbox_names=tuple(names),
        cleanup_error="; ".join(cleanup_errors) or None,
    )
    if not result.passed:
        return result
    event_id = append_runtime_egress_certification(
        store,  # type: ignore[arg-type]
        certification,
        actor=egress_certification_actor(OPENSHELL_RUNTIME),
        repo=repo,
    )
    return dataclasses.replace(result, event_id=event_id)


def _require_shipped_launcher(observed: set[str], shipped: str) -> str:
    """The one launcher every probe read in the image, once it is kew's own.

    An image whose launcher is not the one this kew ships would be certified
    for a program admission never matched — admission reads this host's copy
    beside the image ID — so it is refused with the remedy, not recorded.
    """
    if len(observed) != 1:
        raise EgressProbeError(
            "the egress probes read different launchers in the same image"
        )
    [digest] = observed
    if digest != shipped:
        raise EgressProbeError(
            f"the image's {LAUNCHER_PATH} (sha256:{digest}) is not the launcher "
            f"this kew ships (sha256:{shipped}); rebuild the image with "
            "`kew sandbox build` and certify again"
        )
    return digest


def _delete_probe_sandbox(client: OpenShellClient, name: str) -> str | None:
    """Delete the probe sandbox, returning why it survived when it did.

    An absent sandbox is the goal, not a failure: a gateway that already reaped
    it answers ``not found``, and reporting that as a surviving container would
    send an operator looking for something nobody can delete. Every other
    failure is returned, because an undeleted probe sandbox is a container kew
    created and no longer owns.
    """
    try:
        client.sandbox_delete(name)
    except OpenShellNotFound:
        # Recorded, not swallowed. Nothing survives, so this is not a cleanup
        # failure — but kew created that sandbox and expected to be the one
        # deleting it, and a gateway reaping it first is a fact about the
        # gateway rather than something to infer later from an absence.
        _logger.warning(
            "openshell egress probe sandbox %s was already gone at cleanup; "
            "the gateway reaped it before kew did",
            name,
        )
        return None
    except OpenShellError as exc:
        return exception_text(exc)
    return None
