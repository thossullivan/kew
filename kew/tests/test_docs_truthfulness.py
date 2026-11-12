"""Truthfulness assertions for user-facing documentation.

String-level tests: match distinctive tokens (not full sentences) so they are
robust to minor wording changes while still catching missing coverage.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import tomllib
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from html.parser import HTMLParser
from pathlib import Path

import pytest
from support import source_ast
from support.tool_pins import tool_pins

REPO_ROOT = Path(__file__).parent.parent.parent
# The owner-approved public design pages (2026-09-22). scripts/check-docs.sh
# holds the same allowlist; a new public design page needs both edits.
PUBLIC_DESIGN_PAGES = (
    "2026-06-25-audit-chain-design.md",
    "2026-06-28-cache-aware-routing-design.md",
    "2026-07-01-epic-dag-design.md",
    "2026-07-10-launch-hardening-design.md",
    "2026-07-10-sandboxed-dispatch-design.md",
    "2026-07-23-adversarial-review-stage.md",
    "2026-08-06-host-owned-publication-broker.md",
    "2026-08-17-kew-product-charter.md",
    "2026-08-17-production-certified-customer-journeys.md",
    "2026-08-21-run-report-sink-design.md",
)


@pytest.mark.policy
def test_public_design_allowlist_matches_the_gate_and_the_tree() -> None:
    gate = _read("scripts/check-docs.sh")
    block = re.search(r"PUBLIC_DESIGN_PAGES=\(\n(.*?)\n\)", gate, re.S)
    assert block is not None, "check-docs.sh lost its design allowlist"
    assert tuple(block.group(1).split()) == PUBLIC_DESIGN_PAGES
    tree = tuple(sorted(p.name for p in (REPO_ROOT / "docs/design").iterdir()))
    assert tree == PUBLIC_DESIGN_PAGES


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text()


PROXY_ENCODING_GRAMMAR = (
    "Stage zero uses per-character NFKC normalization "
    "and removes Unicode Cf format characters.",
    "Four further positional stages decode percent bytes in UTF-8 and Latin-1 views, "
    "with any hexadecimal case.",
    "Each stage preserves literal-plus and space-plus views.",
    r"Python and JSON escapes include `\\`, `\xHH`, `\uHHHH`, joined surrogate pairs, "
    r"`\UHHHHHHHH`, `\N{NAME}`, octal, and line continuations.",
    "Case-insensitive hexadecimal patterns match UTF-8 bytes "
    "without positional hexadecimal decoding.",
    "Standard and URL-safe base64 patterns cover three byte alignments, "
    "whitespace wrapping, and optional padding.",
)


def _assert_proxy_encoding_grammar(text: str) -> None:
    from kew import sandbox

    start = "<!-- proxy-encoding-grammar:start -->"
    end = "<!-- proxy-encoding-grammar:end -->"
    assert start in text and end in text, "missing closed encoding grammar"
    section = text.split(start)[1].split(end)[0]
    assert "The accepted encoding grammar is this closed list:" in section
    assert tuple(re.findall(r"^- (.+)$", section, re.MULTILINE)) == (
        PROXY_ENCODING_GRAMMAR
    )
    for non_goal in ("UTF-16 text", "quoted-printable", "HTML entities", "compression"):
        assert non_goal in section, non_goal
    assert "at most four further stages" in text
    assert sandbox.credential_redaction.DECODE_PASSES == 4
    assert sandbox._REDACT_PERCENT_DECODE_PASSES == 4
    assert sandbox.MAX_REDACTED_FIELD_CHARS == 65_536


def test_round25_security_binds_closed_proxy_encoding_grammar() -> None:
    _assert_proxy_encoding_grammar(_read("docs/security.md"))


def test_sandbox_proxy_environment_is_documented() -> None:
    text = _read("docs/configuration.md")
    assert '**`proxy_environment`** (default `"none"`)' in text
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    ):
        assert f"`{name}`" in text
    assert '`"inherit"`' in text
    assert "`proxy_variables`" in text
    assert "`credential_names`" in text
    assert "names only, never proxy values" in text
    _assert_proxy_redaction_configuration(text)


def _assert_proxy_redaction_configuration(text: str) -> None:
    from kew import sandbox

    section = text.split('**`proxy_environment`** (default `"none"`)')[1].split(
        "- **`on_missing_runtime`**"
    )[0]
    normalized = " ".join(section.split())
    for stale in (
        "replace the complete field",
        "replaces the complete field",
        "field becomes empty",
        "[REDACTED]",
        "more than eight times",
        "use one substitution pass",
    ):
        assert stale not in normalized, f"obsolete whole-field claim: {stale}"
    for rule in (
        "after four stages from the original spelling",
        "every intermediate spelling",
        "both literal-plus and space-plus decoding variants",
        "Component selection and credential-name classification "
        "use the same bounded stages",
        "credential-key name at any stage",
        "shortest bounded percent-decoded length",
        "six or more decoded characters match substrings",
        "ASCII alphanumeric token boundaries",
        "Short provenance-only ASCII numbers remain visible",
        "Short forms embedded inside longer alphanumeric tokens remain visible",
        "local replacement, with no occurrence-count limit",
        "Replacements preserve unrelated text",
        "at most three additional substitution passes",
        "local single-character marker, starting with `█`",
        "prevents matches across replacement boundaries",
        "65,536 characters after replacement",
    ):
        assert rule in normalized, f"missing proxy redaction rule: {rule}"
    assert sandbox._REDACT_MIN_CHARS == 6
    assert sandbox._REDACT_PERCENT_DECODE_PASSES == 4
    assert sandbox.MAX_REDACTED_FIELD_CHARS == 65_536


def test_round19_credential_rules_match_documented_constants() -> None:
    from kew import sandbox

    text = _read("docs/security.md")
    assert hasattr(sandbox, "OPAQUE_CREDENTIAL_METRIC_WORDS")
    for label, expected in (
        ("stems", sandbox.OPAQUE_CREDENTIAL_STEMS),
        ("key-qualifiers", sandbox.OPAQUE_CREDENTIAL_KEY_QUALIFIERS),
        ("metric-words", sandbox.OPAQUE_CREDENTIAL_METRIC_WORDS),
    ):
        section = text.split(f"<!-- opaque-credential-{label}:start -->")[1].split(
            f"<!-- opaque-credential-{label}:end -->"
        )[0]
        assert set(re.findall(r"`([a-z]+)`", section)) == expected
    inflections = text.split("<!-- opaque-credential-inflections:start -->")[1].split(
        "<!-- opaque-credential-inflections:end -->"
    )[0]
    assert f"`{sandbox.OPAQUE_CREDENTIAL_DIGIT_SUFFIX}`" in inflections
    assert tuple(re.findall(r"`(ies|es|s)` -> `([^`]*)`", inflections)) == (
        sandbox.OPAQUE_CREDENTIAL_PLURAL_SUFFIXES
    )
    assert "Strip one trailing digit run before testing plural suffixes." in inflections
    assert "Keep the unchanged word and test each suffix once." in inflections
    assert "Numbers, booleans, and null values retain their types and values." in text
    assert "except short provenance-only ASCII numbers" in text
    assert (
        "Short forms embedded inside longer alphanumeric text remain visible "
        "when every stage lacks a boundary." in text
    )
    assert "Shannon" not in text


@pytest.mark.parametrize("policy", ["none", "inherit"])
def test_round14_extraction_proxy_documentation_matches_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, policy: str
) -> None:
    from kew.sandbox import CONTAINER_PROXY_NAMES, build_extract_argv

    for name in CONTAINER_PROXY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://user:secret@proxy")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "runner-credential")
    argv = build_extract_argv(
        runtime="docker",
        image="test:image",
        checkout_dir=tmp_path / "checkout",
        artifact_dir=tmp_path / "artifact",
        slug="round14",
        base_sha="a" * 40,
        uid=501,
        gid=20,
    )
    env = [argv[index + 1] for index, arg in enumerate(argv) if arg == "-e"]
    actual = [item.partition("=")[0] for item in env if item != "HOME=/tmp"]
    text = _read("docs/sandbox-entrypoint-contract.md")
    section = text.split("<!-- extraction-proxy-variables:start -->")[1].split(
        "<!-- extraction-proxy-variables:end -->"
    )[0]
    assert re.findall(r"`([A-Za-z_]+)`", section) == actual
    _assert_round14_extraction_environment(argv, env, actual, policy)
    assert (
        "Runner credentials never enter the extraction environment under either policy."
        in text
    )


def _proxy_names():
    """The exact names these launches assign, in the order the argv uses."""
    from kew.credential_surface import _PROXY_BUILD_NAMES

    return list(_PROXY_BUILD_NAMES)


def _assert_round14_extraction_environment(argv, env, actual, policy):
    """Extraction carries --network none, so every proxy name is blanked.

    `policy` is the operator's configured posture for the agent container; it
    does not reach this launch (governed review round 4, 2026-09-21).
    """
    assert policy in ("none", "inherit")
    assert actual == _proxy_names()
    for name in _proxy_names():
        assert f"{name}=" in env
        assert name not in env
    assert all("secret" not in argument for argument in argv)
    assert "ANTHROPIC_API_KEY" not in env
    assert "runner-credential" not in " ".join(argv)


def _assert_checkout_proxy_security_documentation(env, policy):
    text = _read("docs/security.md")
    assert (
        "Both explicitly control ten proxy variables in uppercase and lowercase."
        in text
    )
    assert "Both blank all ten under either policy: they carry `--network none`" in text
    assert "`allProxy`" in text
    # `policy` is the operator's configured posture; these launches blank every
    # name whatever it says (governed review round 4, 2026-09-21).
    assert policy in ("none", "inherit")
    for name in _proxy_names():
        assert f"{name}=" in env
        assert name not in env


@pytest.mark.parametrize("policy", ["none", "inherit"])
def test_checkout_proxy_security_documentation_matches_argv(
    monkeypatch, tmp_path, policy
):
    from kew import sandbox

    for name in sandbox._PROXY_BUILD_NAMES:
        monkeypatch.setenv(name, "https://proxy.invalid/fixture")
    argv = sandbox.build_checkout_program_argv(
        runtime="docker",
        image="test:image",
        checkout_dir=tmp_path,
        slug="docs",
        program=["/bin/true"],
        uid=501,
        gid=20,
    )
    env = [argv[index + 1] for index, arg in enumerate(argv) if arg == "-e"]
    _assert_checkout_proxy_security_documentation(env, policy)


@pytest.mark.policy
def test_round15_documented_packaged_pins_match_packaged_inputs():
    from kew.sandbox import (
        _expected_packaged_image_pin,
        packaged_agent_image_context,
    )

    context = packaged_agent_image_context()
    expected = {
        _expected_packaged_image_pin(context, runner)
        for runner in ("claude-code", "codex", "opencode")
    }
    observed = set()
    violations = []
    for page in (REPO_ROOT / "docs").rglob("*.md"):
        for pin in re.findall(
            r"\bkew-agent(?:-[\w-]+)?:docker-[\w]+", page.read_text()
        ):
            observed.add(pin)
            if pin not in expected:
                violations.append(f"{page.relative_to(REPO_ROOT)}: {pin}")
    assert not violations, "\n".join(violations)
    assert expected <= observed, "Document both packaged runner pins."


@pytest.mark.policy
def test_documented_routing_models_have_catalog_rows() -> None:
    """Every documented ``[routing]`` lane model has a catalog row (#2709).

    ``load_config`` refuses a routed model with no lifecycle row, so a docs
    example routing an id the catalog does not name is a config that fails to
    load. Free local models (``ollama/*``) are exempt, as at load time.
    """
    from kew.model_catalog import load_catalog
    from kew.pricing import is_free_model

    rows = {row.id for row in load_catalog().rows}
    violations = []
    for page in sorted((REPO_ROOT / "docs").rglob("*.md")):
        for block in re.findall(r"```toml\n(.*?)```", page.read_text(), re.S):
            if "[routing]" not in block:
                continue
            for lane, entry in tomllib.loads(block).get("routing", {}).items():
                model = entry.get("model") if isinstance(entry, dict) else None
                if model and model not in rows and not is_free_model(model):
                    violations.append(f"{page.relative_to(REPO_ROOT)}: {lane}={model}")
    assert not violations, "\n".join(violations)


def _replay_checkout_changes(repo: Path) -> None:
    """Apply the checkout's uncommitted tracked changes to a HEAD clone.

    The docs gate must be tested against the candidate tree, not only HEAD:
    a change that deletes pages and edits the gate in one pass would
    otherwise run the new gate over the old tree. On a clean checkout (CI)
    the diff is empty and the clone is HEAD exactly.
    """
    diff = subprocess.run(
        ["git", "diff", "--binary", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout
    if diff:
        # --index: the gate reads the index for the non-docs prefixes.
        subprocess.run(["git", "apply", "--index"], cwd=repo, input=diff, check=True)


def test_sidecar_docs_match_enforced_evidence() -> None:
    security = _read("docs/security.md").casefold()
    audit = _read("docs/audit.md")

    for claim in (
        "direct container routes with ambient keys use "
        "`env:<name>` provider authentication.",
        "packet-outcome matrix",
        "positive controls",
        "profile fingerprint",
        "successful cleanup",
        "production sidecar mode remains disabled",
        "W1.G",
    ):
        assert claim.casefold() in security
    for field in (
        "sidecar_isolation_evidence",
        "worker_id",
        "proxy_id",
        "network_id",
        "packet_verdict",
        "cleanup_verdict",
        "runtime_egress_certification",
        "openshell-egress-gate",
    ):
        assert field in audit


def test_security_docs_state_credential_and_egress_residuals() -> None:
    security = " ".join(_read("docs/security.md").casefold().split())
    audit = " ".join(_read("docs/audit.md").casefold().split())

    for claim in (
        "claude_code_oauth_token",
        "anthropic_api_key",
        "native codex chatgpt login",
        "openrouter_api_key",
        "synthetic per-attempt",
        "credential reusable after the run?",
        "kew bounds issuer spend and expiry?",
        "compensating control",
        "synthetic worker value: no. proxy provider credential: yes.",
        "openrouter_provisioning_key",
        "yes — per-run key with a finite no-reset limit and a utc expiry, "
        "revoked at run end; identity recorded, value never",
        "issued:openrouter:<hash>",
        "`sidecar-certified` comes only after matching packet certification "
        "passes sidecar admission.",
        "`openshell-certified` comes only after a passing openshell egress "
        "certification matches the exact boundary tuple at admission.",
        "`operator-managed-unverified` permanently covers every direct contained "
        'route, including `network = "open"`.',
        "`unavailable` covers a refused route, non-container route, or sidecar "
        "awaiting certification.",
        "iptables rules you add",
    ):
        assert claim in security

    for claim in (
        "2026-09-13",
        "egress_verification",
        "additive attested fact",
        "hashed payload",
        "existing hashes stand",
    ):
        assert claim in audit


def test_policy_less_runners_are_documented_under_their_own_refusal() -> None:
    """The three pages agree on who has an OpenShell policy, and on the token.

    `pi` and `hermes` have no packaged policy, so `kew sandbox certify` cannot
    prove a boundary for them and the docs must not point at it: they are
    refused as `openshell-policy-unpackaged`. The runner list is read off the
    code rather than retyped, so packaging a fourth policy fails here instead
    of leaving three pages naming three (#2435).
    """
    from kew.openshell_egress import packaged_openshell_policy_runners
    from kew.sandbox import REFUSAL_OPENSHELL_POLICY_UNPACKAGED

    packaged = packaged_openshell_policy_runners()
    pages = {
        rel: " ".join(_read(rel).split())
        for rel in ("docs/security.md", "docs/cli/sandbox.md", "docs/audit.md")
    }

    for rel, text in pages.items():
        assert REFUSAL_OPENSHELL_POLICY_UNPACKAGED in text, rel
        # Named as the runners the gate refuses, not as ones kew certifies.
        assert "`pi`" in text, rel
        assert "`hermes`" in text, rel
    # The two pages that enumerate who kew DOES package a policy for; the audit
    # page documents the refusal reason, not the catalogue.
    for rel in ("docs/security.md", "docs/cli/sandbox.md"):
        for runner in packaged:
            assert f"`{runner}`" in pages[rel], (rel, runner)
    for runner in ("pi", "hermes"):
        assert runner not in packaged


def test_security_page_still_states_the_missing_openshell_backend() -> None:
    """The backend claim is only removable when the backend exists.

    `docs/security.md` once said a certified OpenShell boundary refuses with
    `openshell-runtime-unavailable` because this build has no backend, and that
    sentence is true exactly while `require_launchable_runtime` rejects the
    runtime. Bound rather than left to a reader: #2435 proposed deleting it
    against a launch lifecycle that was not yet in the build, which would have
    made the security page claim containment kew could not provide. The
    witness is the LAUNCH gate, not `require_backed_runtime`: since #2320 kew
    launches into an OpenShell sandbox while still being unable to inspect one
    from a run row, and the sentence is about launching.
    """
    from kew.runtime_names import (
        OPENSHELL_RUNTIME,
        RuntimeBackendUnavailable,
        require_launchable_runtime,
    )

    security = " ".join(_read("docs/security.md").split())
    try:
        require_launchable_runtime(OPENSHELL_RUNTIME)
    except RuntimeBackendUnavailable:
        assert "This build still has no OpenShell backend" in security
        assert "openshell-runtime-unavailable" in security
        return
    assert "This build still has no OpenShell backend" not in security
    assert "launches through the gateway" in security


def test_posture_record_and_surfaces_are_documented() -> None:
    security = _read("docs/security.md")
    threat = _read("docs/threat-model.md")
    audit = _read("docs/audit.md")
    combined = "\n".join((security, threat, audit))

    for field in (
        "filesystem_boundary",
        "network_boundary",
        "credential_names",
        "command_guard",
        "admission",
    ):
        assert field in combined
    for surface in ("kew status --run", "Serve", "TUI", "evidence pack"):
        assert surface.casefold() in combined.casefold()
    assert "posture: not recorded" in security
    assert "posture: not recorded" in threat
    assert (
        "File stores read rows and posture events through a separate read-only "
        "connection in one committed snapshot."
    ) in security
    assert "2026-09-13 — complete sandbox posture boundary added" in audit


def test_remote_placement_attestation_is_a_section_of_its_own() -> None:
    """It was inserted mid-paragraph, under the heading above its own.

    Six closing lines of the `sandbox_resolved` paragraph — and the two dated
    entries after it — ended up beneath the remote-placement heading, where they
    restated that section's own last sentence and attributed the `run_id` and
    surface claims to `dispatch_remote_launch` (#2437 review).
    """

    def section(heading: str) -> str:
        audit = _read("docs/audit.md")
        assert heading in audit, heading
        return audit.split(heading, 1)[1].split("\n## ", 1)[0]

    posture = section("## Sandbox posture attestation")
    remote = section("## Remote placement attestation")

    assert "dispatch_remote_launch" in remote
    for line in (
        "They bind through the existing hashed payload.",
        "New dispatch events also carry `run_id` for an exact surface join.",
        "They show `posture: not recorded` when the event is absent.",
        "2026-10-06 — effective resource caps added",
    ):
        assert line in posture, line
        assert line not in remote, line
    # One closing claim, stated once: the restatement was the duplicate.
    assert remote.count("bind through the hashed `payload`") == 1
    assert "existing hashed payload" not in remote


def test_direct_egress_route_rows_match_permanent_acceptance() -> None:
    """Every direct credential row carries the exact accepted egress claim."""
    from kew import sandbox

    security = _read("docs/security.md")
    rows = [
        line
        for line in security.splitlines()
        if line.startswith(("| Contained ", "| Direct OpenRouter with "))
    ]
    assert len(rows) == 2
    # Compare the entire control cell so an added stronger claim fails too.
    for row in rows:
        assert "Direct egress is operator-asserted." in row
        bounded = row.startswith("| Direct OpenRouter with ")
        control = (
            "The host issues and revokes the key. "
            "Publication waits for recorded successful revocation. "
            if bounded
            else "The container receives only the runner credential. "
            "Publication stays host-only. Exposure lineage blocks publication. "
        )
        assert row.split("|")[-2].strip() == (
            control
            + "Direct egress is operator-asserted. Kew permanently records "
            + f"`{sandbox.DIRECT_ROUTE_EGRESS_VERIFICATION}`"
            + (" for contained routes." if bounded else ".")
        )
    assert "kew installs and verifies no destination rules there." in security
    for document in (security, _read("docs/threat-model.md")):
        assert "#1945" in document
        assert (
            f"Kew permanently records `{sandbox.DIRECT_ROUTE_EGRESS_VERIFICATION}` "
            "for every direct contained route."
        ) in document
        assert (
            "Operators who need kew-verified egress use the sidecar route" in document
        )


# Archived-path fragments, synthesized so this file never carries a literal
# reference the docs gate refuses.
_AUDIT = "audit" + "/"
_DESIGN = "design" + "/"
_PACKS = "evidence-packs" + "/"
_BLOB = "https://github.com/thossullivan/kew/blob/main/"
_AT_CUT = "@" + "1702613a"


def _scratch_repo(tmp_path: Path) -> Path:
    """Clone the checkout's HEAD locally for a docs-gate defeating-input test.

    Depth 1 over ``file://``, never ``--shared``: a ``--shared`` clone borrows
    every reachable object through alternates, and a self-hosted runner's
    persistent checkout can be a partial (promisor) clone whose history is
    not all local — ``upload-pack`` then aborts (main red on cb72f3fd, one
    runner only). HEAD's tree is always present, and no gate test needs more
    than the tip plus the commits it makes itself.
    """
    scratch = tmp_path / "repo"
    subprocess.run(
        [
            "git",
            "-c",
            "advice.detachedHead=false",
            "clone",
            "--quiet",
            "--depth",
            "1",
            f"file://{REPO_ROOT}",
            str(scratch),
        ],
        check=True,
    )
    _replay_checkout_changes(scratch)
    return scratch


def _run_docs_gate(repo: Path, **env: str) -> subprocess.CompletedProcess[str]:
    # Hermetic: the gate reads GITHUB_EVENT_NAME to pick its diff base, and on a
    # push to main it chooses HEAD^ — which a depth-1 CI checkout (and therefore
    # its --shared scratch clone) does not have, so the gate failed loud before
    # the stage under test (main red on 54d14cd4). The scratch repo is not the
    # CI event; drop the Actions env and pin the base to HEAD (no deletions →
    # the basename stage is a no-op) unless the test pins its own.
    command_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("GITHUB_", "RUNNER_", "CHECK_DOCS_"))
    }
    # Base = HEAD makes the basename (deletion) stage a no-op by construction;
    # the two tests that exercise it pass their own base through **env.
    command_env["CHECK_DOCS_DIFF_BASE"] = "HEAD"
    command_env.update(env)
    return subprocess.run(
        ["bash", "scripts/check-docs.sh"],
        cwd=repo,
        env=command_env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )


def _shallow_main_clone(tmp_path: Path) -> Path:
    """The CI `test` job's checkout shape: one commit, `.git/shallow`, and an
    `origin/main` equal to HEAD — the shape that made the gate pick `HEAD^`.

    Clones the checkout's HEAD (whatever branch, or detached, it is on) and
    then names it `origin/main`, so the test runs on PR CI too — a PR checkout
    is a detached merge ref with no local `main` to clone by name.
    """
    scratch = tmp_path / "shallow"
    subprocess.run(
        [
            "git",
            "-c",
            "advice.detachedHead=false",
            "clone",
            "--quiet",
            "--depth",
            "1",
            f"file://{REPO_ROOT}",
            str(scratch),
        ],
        check=True,
    )
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
        cwd=scratch,
        check=True,
    )
    _replay_checkout_changes(scratch)
    return scratch


def _one_commit_clone(tmp_path: Path) -> Path:
    """The public repository's first push: the candidate tree as a `git
    archive` snapshot committed as one root commit, with `origin/main` equal
    to it. Not shallow, so HEAD^ is missing because HEAD is a root."""
    scratch = _scratch_repo(tmp_path)
    tree = subprocess.run(
        ["git", "write-tree"], cwd=scratch, check=True, capture_output=True, text=True
    ).stdout.strip()
    archive = subprocess.run(
        ["git", "archive", "--format=tar", tree],
        cwd=scratch,
        check=True,
        capture_output=True,
    ).stdout
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    subprocess.run(["tar", "-x", "-C", str(snapshot)], input=archive, check=True)
    identity = ["-c", "user.name=snapshot", "-c", "user.email=snapshot@example.invalid"]
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "-A", "--force"],
        [*identity, "commit", "-q", "-m", "snapshot"],
        ["update-ref", "refs/remotes/origin/main", "HEAD"],
    ):
        subprocess.run(["git", *args], cwd=snapshot, check=True, capture_output=True)
    return snapshot


class _ResourceTags(HTMLParser):
    """Collect what a built page would fetch, and Material's source widget."""

    # `<link>` rels that name a URL without the browser fetching it.
    _NAVIGATIONAL_RELS = frozenset({"canonical", "alternate", "next", "prev"})

    def __init__(self) -> None:
        super().__init__()
        self.fetched: list[str] = []
        self.source_widgets = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if values.get("data-md-component") == "source":
            self.source_widgets += 1
        if tag == "link" and values.get("rel") not in self._NAVIGATIONAL_RELS:
            self.fetched.append(values.get("href", ""))
        elif tag in {"script", "img", "iframe", "source", "video", "audio", "embed"}:
            self.fetched.append(values.get("src", ""))


def _is_off_origin(url: str) -> bool:
    return bool(re.match(r"(?i)^([a-z][a-z0-9+.-]*:)?//", url))


def test_docs_site_loads_no_third_party_resource(tmp_path: Path) -> None:
    # #1168: Material's `theme.font` was a Google Fonts request and
    # `repo_url` a GitHub API source widget, both blocked by the site CSP
    # (`default-src 'self'`). The real docs, built through the gate, must
    # fetch nothing off origin and render in IBM Plex served from the site.
    repo = _scratch_repo(tmp_path)

    result = _run_docs_gate(repo)
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    site = repo / "site"
    pages = sorted(site.rglob("*.html"))
    assert pages, output
    off_origin: list[str] = []
    widgets = 0
    for page in pages:
        tags = _ResourceTags()
        tags.feed(page.read_text(encoding="utf-8"))
        off_origin += [
            f"{page.relative_to(site)}: {url}"
            for url in tags.fetched
            if _is_off_origin(url)
        ]
        widgets += tags.source_widgets
    assert not off_origin, "\n".join(off_origin)
    assert widgets == 0

    css_path = site / "stylesheets/extra.css"
    css = css_path.read_text(encoding="utf-8")
    faces = re.findall(r"@font-face\s*\{([^}]*)\}", css)
    declared = set()
    for face in faces:
        family = re.search(r"font-family:\s*\"([^\"]+)\"", face)
        weight = re.search(r"font-weight:\s*(\d+)", face)
        assert family and weight, face
        declared.add((family.group(1), weight.group(1)))
        urls = re.findall(r"url\(\s*[\"']?([^\"')]+)", face)
        assert urls, face
        for url in urls:
            assert not _is_off_origin(url), url
            served = (css_path.parent / url).resolve()
            assert served.is_relative_to(site.resolve()), url
            assert served.is_file(), url
    assert declared == {
        (family, weight)
        for family in ("IBM Plex Sans", "IBM Plex Mono")
        for weight in ("400", "600")
    }
    # The site serves the copy kew vendors, not a second one. Read from the
    # scratch clone the site was built from, not the checkout.
    vendored = sorted((repo / "kew/src/kew/assets/fonts").glob("*.woff2"))
    assert vendored
    for font in vendored:
        assert (site / "assets/fonts" / font.name).read_bytes() == font.read_bytes()


def test_plain_mkdocs_build_leaves_an_audit_pack_out_of_the_site(
    tmp_path: Path,
) -> None:
    # #1993 (3): the documented deploy runs a plain `mkdocs build`, never the
    # gate, so `exclude_docs` alone must keep an operator's untracked audit
    # pack out of the published site.
    repo = _scratch_repo(tmp_path)
    pack = repo / "docs" / _AUDIT / "2026-01-01-scratch-held-pack.md"
    pack.parent.mkdir(parents=True)
    pack.write_text("# Scratch held pack\n")
    pins = tool_pins()

    result = subprocess.run(
        [
            "uvx",
            "--from",
            f"mkdocs=={pins['MKDOCS_VERSION']}",
            "--with",
            f"mkdocs-material=={pins['MKDOCS_MATERIAL_VERSION']}",
            "mkdocs",
            "build",
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert (repo / "site/audit/index.html").is_file(), output
    leaked = [
        path
        for path in (repo / "site").rglob("*")
        if "2026-01-01-scratch-held-pack" in path.name
    ]
    assert not leaked, leaked


class TestDocsGateDefeatingInputs:
    def test_gate_helper_ignores_the_ci_event_env_in_a_shallow_clone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Main went red on 54d14cd4: the helper inherited GITHUB_EVENT_NAME=push
        # from CI's depth-1 checkout, the gate chose HEAD^, and failed loud on
        # the base before the stage under test. The helper must run the gate
        # against the fixture, whatever the process env says about the event.
        if not (REPO_ROOT / ".git").exists():
            pytest.skip("not a git checkout")
        repo = _shallow_main_clone(tmp_path)
        monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        monkeypatch.setenv("CHECK_DOCS_DIFF_BASE", "bogus")
        with (repo / "docs/index.md").open("a") as page:
            page.write(
                "\n[Scratch excluded link](audit/2026-01-01-scratch-held-pack.md)\n"
            )

        result = _run_docs_gate(repo)
        output = result.stdout + result.stderr

        assert "does not resolve to a commit" not in output, output
        assert result.returncode != 0, output
        assert "2026-01-01-scratch-held-pack.md" in output, output

    def test_the_first_push_of_a_one_commit_repository_passes(
        self, tmp_path: Path
    ) -> None:
        # The public repository starts as one `git archive` commit. On its
        # first push the gate picked HEAD^, which a root commit lacks, and
        # failed on the base before any stage ran.
        repo = _one_commit_clone(tmp_path)

        result = _run_docs_gate(repo, GITHUB_EVENT_NAME="push", CHECK_DOCS_DIFF_BASE="")
        output = result.stdout + result.stderr

        assert "does not resolve to a commit" not in output, output
        assert "HEAD is a root commit: comparing with the empty tree" in output
        assert result.returncode == 0, output

    def test_a_root_commit_still_refuses_what_the_gate_refuses(
        self, tmp_path: Path
    ) -> None:
        # The empty-tree base must not wave a root commit through: a stage
        # after the base still fails it.
        repo = _one_commit_clone(tmp_path)
        with (repo / "docs/index.md").open("a") as page:
            page.write("\n[Scratch excluded link](brand-guidelines.md)\n")

        result = _run_docs_gate(repo, GITHUB_EVENT_NAME="push", CHECK_DOCS_DIFF_BASE="")
        output = result.stdout + result.stderr

        assert "HEAD is a root commit: comparing with the empty tree" in output
        assert result.returncode != 0, output
        assert "brand-guidelines.md" in output, output

    def test_design_and_public_audit_page_build_without_internal_pages(
        self, tmp_path: Path
    ) -> None:
        repo = _scratch_repo(tmp_path)

        result = _run_docs_gate(repo)
        output = result.stdout + result.stderr

        assert result.returncode == 0, output
        assert (
            repo / "site/design/2026-08-17-kew-product-charter/index.html"
        ).is_file()
        assert (repo / "site/audit/index.html").is_file()
        built_design = sorted(
            path.parent.name for path in (repo / "site/design").glob("*/index.html")
        )
        assert built_design == sorted(
            name.removesuffix(".md") for name in PUBLIC_DESIGN_PAGES
        ), output
        assert not list((repo / "site/audit").glob("2026-*")), output

    def test_link_from_built_page_to_excluded_page_fails(self, tmp_path: Path) -> None:
        repo = _scratch_repo(tmp_path)
        assert (repo / "docs/brand-guidelines.md").is_file()
        with (repo / "docs/index.md").open("a") as page:
            page.write("\n[Scratch excluded link](brand-guidelines.md)\n")

        result = _run_docs_gate(repo)
        output = result.stdout + result.stderr

        assert result.returncode != 0, (
            "check-docs.sh accepted a built page linking to an excluded page"
        )
        assert "brand-guidelines.md" in output, output

    def test_raw_html_link_to_excluded_audit_pack_fails(self, tmp_path: Path) -> None:
        repo = _scratch_repo(tmp_path)
        with (repo / "docs/index.md").open("a") as page:
            page.write(
                '\n<a href="audit/2026-01-01-scratch-held-pack.md">'
                "Scratch excluded link</a>\n"
            )

        result = _run_docs_gate(repo)
        output = result.stdout + result.stderr

        assert result.returncode != 0, (
            "check-docs.sh accepted a raw link to an excluded audit pack"
        )
        assert "built pages carry hrefs into an excluded path" in output, output

    @pytest.mark.parametrize(
        ("relative", "message"),
        [
            # Synthesized paths throughout: a literal would trip the gate's own
            # reference scans over this file.
            (
                "docs/" + _AUDIT + "2026-09-22-scratch.md",
                "docs/audit is back in the tree",
            ),
            ("kew/docs/" + "plans/scratch.md", "kew/docs/plans is back in the tree"),
            ("kew/docs/" + "issues/scratch.md", "kew/docs/issues is back in the tree"),
            (_PACKS + "scratch.html", "evidence-packs is back in the tree"),
            (".kew/" + "epic-plan-scratch.json", "epic-plan-scratch.json is internal"),
            (
                "docs/" + _DESIGN + "2026-09-22-scratch-design.md",
                "2026-09-22-scratch-design.md is not on the public",
            ),
        ],
    )
    def test_returning_internal_material_is_rejected(
        self, tmp_path: Path, relative: str, message: str
    ) -> None:
        repo = _scratch_repo(tmp_path)
        returned = repo / relative
        returned.parent.mkdir(parents=True, exist_ok=True)
        returned.write_text("# Scratch internal material\n")
        # Staged, as a commit would carry it: the non-docs prefixes are
        # checked in the index so local untracked scratch does not fail.
        subprocess.run(["git", "add", "--force", relative], cwd=repo, check=True)

        result = _run_docs_gate(repo)
        output = result.stdout + result.stderr

        assert result.returncode != 0, f"check-docs.sh accepted {relative}"
        assert message in output, output
        assert "✓ no deleted internal tree returned" not in output, output

    @pytest.mark.parametrize(
        "form",
        ["docs-page code span", "built-page raw href", "published design page"],
    )
    @pytest.mark.parametrize(
        ("path", "href"),
        [
            (
                "docs/" + _AUDIT + "2026-08-06-feature" + "-census.md",
                _AUDIT + "2026-08-06-feature" + "-census.html",
            ),
            (
                "kew/docs/" + "plans/2026-07-23-reviewer-path-convergence.md",
                _BLOB + "kew/docs/" + "plans/2026-07-23-reviewer-path-convergence.md",
            ),
            (
                "kew/docs/" + "issues/t2-lsp-mcp-integration.md",
                _BLOB + "kew/docs/" + "issues/t2-lsp-mcp-integration.md",
            ),
            (
                _PACKS + "2026-07-26-launch.html",
                "/" + _PACKS + "2026-07-26-launch.html",
            ),
            (
                ".kew/" + "epic-plan-product-coherence.json",
                _BLOB + ".kew/" + "epic-plan-product-coherence.json",
            ),
            (
                "docs/" + _DESIGN + "2026-07-26-containment" + "-epic.md",
                _DESIGN + "2026-07-26-containment" + "-epic.md",
            ),
        ],
    )
    def test_reference_into_archived_material_fails(
        self, tmp_path: Path, path: str, href: str, form: str
    ) -> None:
        # The launch cut archived these at 1702613a. A reference to one — a code
        # span in a docs page, or a raw href that mkdocs publishes verbatim into
        # the built page — must fail the gate after the cut lands on main, when
        # the deletion-diff basename stage no longer sees it. The ten kept
        # design pages are published, so a reference written on one fails too.
        repo = _scratch_repo(tmp_path)
        page_path = "docs/index.md"
        reference = f"\nSee `{path}`.\n"
        if form == "built-page raw href":
            reference = f'\n<a href="{href}">Scratch archived link</a>\n'
        elif form == "published design page":
            page_path = "docs/" + _DESIGN + PUBLIC_DESIGN_PAGES[-1]
        with (repo / page_path).open("a") as page:
            page.write(reference)

        result = _run_docs_gate(repo)
        output = result.stdout + result.stderr

        assert result.returncode != 0, f"check-docs.sh accepted {reference!r}"
        assert "docs gate FAILED" in output, output
        assert "✓ no deleted internal tree returned" not in output, output

    def test_history_pointers_into_the_launch_cut_are_accepted(
        self, tmp_path: Path
    ) -> None:
        repo = _scratch_repo(tmp_path)
        with (repo / "docs/index.md").open("a") as page:
            page.write(
                "\nArchived: `docs/"
                + _AUDIT
                + "2026-08-06-feature"
                + "-census.md"
                + _AT_CUT
                + "`, `docs/"
                + _DESIGN
                + "2026-07-26-containment"
                + "-epic.md"
                + _AT_CUT
                + "`.\n"
            )

        result = _run_docs_gate(repo)
        output = result.stdout + result.stderr

        assert result.returncode == 0, output

    @pytest.mark.parametrize(
        "pointer",
        [
            # Typo'd: no such page ever existed at the mirror.
            "2026-07-26-containment" + "-epci.md" + _AT_CUT,
            # Real archived page, but bare: no stage could check the path.
            "2026-07-26-containment" + "-epic.md" + _AT_CUT,
            "2026-08-06-feature" + "-census.md" + _AT_CUT,
        ],
    )
    def test_bare_history_pointer_fails(self, tmp_path: Path, pointer: str) -> None:
        # A shallow scratch clone lacks the mirror commit, so the resolving
        # check is skipped; the full-path requirement must not depend on it.
        repo = _scratch_repo(tmp_path)
        with (repo / "docs/index.md").open("a") as page:
            page.write(f"\nArchived: `{pointer}`.\n")

        result = _run_docs_gate(repo)
        output = result.stdout + result.stderr

        assert result.returncode != 0, f"check-docs.sh accepted {pointer!r}"
        assert "docs gate FAILED" in output, output
        assert "✓ no deleted internal tree returned" not in output, output

    @pytest.mark.parametrize("suffix", ["@deadbeef", "@0123abcd", "@cf9e16da"])
    def test_full_path_pointer_to_an_unvalidated_commit_fails(
        self, tmp_path: Path, suffix: str
    ) -> None:
        # Only the 4a387c0b and 1702613a mirrors are validated; any other hex
        # suffix is an ordinary citation of an archived page.
        repo = _scratch_repo(tmp_path)
        cited = "docs/design/2026-07-26-containment" + "-epic.md" + suffix
        with (repo / "docs/index.md").open("a") as page:
            page.write(f"\nArchived: `{cited}`.\n")

        result = _run_docs_gate(repo)
        output = result.stdout + result.stderr

        assert result.returncode != 0, f"check-docs.sh accepted {cited!r}"
        assert "docs gate FAILED" in output, output

    def test_deleted_doc_basename_cited_elsewhere_fails(self, tmp_path: Path) -> None:
        repo = _scratch_repo(tmp_path)
        deleted = repo / "docs/scratch-deleted-page-1704.md"
        deleted.write_text("# Scratch deletion\n")
        subprocess.run(["git", "add", str(deleted)], cwd=repo, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Docs Gate Test",
                "-c",
                "user.email=docs-gate@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "test fixture",
            ],
            cwd=repo,
            check=True,
        )
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        deleted.unlink()
        with (repo / "kew/src/kew/__init__.py").open("a") as source:
            source.write("\n# See scratch-deleted-page-1704.md\n")

        result = _run_docs_gate(repo, CHECK_DOCS_DIFF_BASE=base)
        output = result.stdout + result.stderr

        assert result.returncode != 0, (
            "check-docs.sh accepted a deleted docs basename still cited in kew/src"
        )
        assert "deleted docs basename 'scratch-deleted-page-1704.md'" in output, output
        assert "kew/src/kew/__init__.py" in output, output

    def test_renamed_doc_basename_cited_elsewhere_fails(self, tmp_path: Path) -> None:
        # `git mv` reports a rename, not a deletion, unless the gate asks for
        # --no-renames; the old basename is still a dangling citation.
        repo = _scratch_repo(tmp_path)
        old = repo / "docs/scratch-renamed-page-1704.md"
        old.write_text("# Scratch rename\n")
        subprocess.run(["git", "add", str(old)], cwd=repo, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "fixture",
            ],
            cwd=repo,
            check=True,
        )
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        subprocess.run(
            [
                "git",
                "mv",
                "docs/scratch-renamed-page-1704.md",
                "docs/scratch-renamed-page-1704-v2.md",
            ],
            cwd=repo,
            check=True,
        )
        with (repo / "README.md").open("a") as readme:
            readme.write("\nSee scratch-renamed-page-1704.md for details.\n")

        result = _run_docs_gate(repo, CHECK_DOCS_DIFF_BASE=base)
        output = result.stdout + result.stderr

        assert result.returncode != 0, (
            "check-docs.sh accepted a git mv that left a cited basename"
        )
        assert "scratch-renamed-page-1704.md" in output, output
        assert "README.md" in output, output

    @pytest.mark.parametrize(
        "tag",
        [
            '<link rel="stylesheet" href="https://example.invalid/scratch.css">',
            '<script src="//example.invalid/scratch.js"></script>',
            "<IMG SRC='https://example.invalid/scratch.png' alt=''>",
            '<img\n  src="https://example.invalid/scratch.multiline.png" alt="">',
            '<link rel="alternate stylesheet" href="https://example.invalid/scratch.css">',
            '<img srcset="/a.png 1x, https://example.invalid/scratch.2x.png 2x">',
            # The URL parser drops tab, newline and CR anywhere in a URL.
            '<img src="ht&#9;tps://example.invalid/scratch.tab.png" alt="">',
            '<img src="https://example.&#10;invalid/scratch.newline.png" alt="">',
            # A scheme with no slashes is still another origin from https.
            '<img src="http:example.invalid/scratch.noslash.png" alt="">',
            '<svg><image href="https://example.invalid/scratch.image.png"/></svg>',
            '<svg><use href="https://example.invalid/scratch.use.svg#i"/></svg>',
            '<svg><use xlink:href="https://example.invalid/scratch.xlink.svg#i"/></svg>',
            '<base href="https://example.invalid/scratch.base/">',
            "<style>.x { background: URL(https://example.invalid/scratch.style.png) }"
            "</style>",
            "<span style=\"background:url('//example.invalid/scratch.attr.png')\">"
            "x</span>",
            # Material's analytics partial builds its script URL inline.
            '<script id="__analytics">var t=document.createElement("script");'
            't.src="https://example.invalid/scratch.gtag.js"</script>',
        ],
    )
    def test_built_page_loading_an_off_origin_resource_fails(
        self, tmp_path: Path, tag: str
    ) -> None:
        # #1168: raw HTML reaches the built page verbatim, and the site CSP
        # blocks it in the browser; the gate has to refuse it first.
        repo = _scratch_repo(tmp_path)
        with (repo / "docs/index.md").open("a") as page:
            page.write(f"\n{tag}\n")

        result = _run_docs_gate(repo)
        output = result.stdout + result.stderr

        assert result.returncode != 0, f"check-docs.sh accepted {tag!r}"
        assert "built pages load an off-origin resource" in output, output
        assert "example.invalid/scratch." in output, output

    @pytest.mark.parametrize(
        ("markup", "reason"),
        [
            # Material's bundle fetches mermaid from unpkg.com for these.
            ('<pre class="mermaid">graph LR; A --> B</pre>', "mermaid"),
            ('<div class="diagram mermaid">graph LR; A --> B</div>', "mermaid"),
            # ...and calls the GitHub API for the repository source widget.
            (
                '<a class="md-source" data-md-component="source" '
                'href="https://github.com/example/scratch">x</a>',
                "source widget",
            ),
        ],
    )
    def test_built_page_triggering_a_material_off_origin_loader_fails(
        self, tmp_path: Path, markup: str, reason: str
    ) -> None:
        # The markup names no off-origin URL itself: Material's JS bundle
        # holds the URL and fetches it when the page carries this markup.
        repo = _scratch_repo(tmp_path)
        with (repo / "docs/index.md").open("a") as page:
            page.write(f"\n{markup}\n")

        result = _run_docs_gate(repo)
        output = result.stdout + result.stderr

        assert result.returncode != 0, f"check-docs.sh accepted {markup!r}"
        assert "built pages load an off-origin resource" in output, output
        assert reason in output, output

    @pytest.mark.parametrize(
        "rule",
        [
            ".x { background: url(https://example.invalid/scratch.plain.png); }",
            '@IMPORT URL("HTTPS://example.invalid/scratch.upper.css");',
            '@import"https://example.invalid/scratch.nospace.css";',
            ".x { background-image: "
            'image-set("https://example.invalid/scratch.set.png" 1x); }',
        ],
    )
    def test_built_stylesheet_loading_an_off_origin_resource_fails(
        self, tmp_path: Path, rule: str
    ) -> None:
        repo = _scratch_repo(tmp_path)
        with (repo / "docs/stylesheets/extra.css").open("a") as css:
            css.write(f"\n{rule}\n")

        result = _run_docs_gate(repo)
        output = result.stdout + result.stderr

        assert result.returncode != 0, f"check-docs.sh accepted {rule!r}"
        assert "built pages load an off-origin resource" in output, output
        assert "example.invalid/scratch." in output, output

    @pytest.mark.parametrize(
        ("source", "script"),
        [
            (
                "javascripts/extra.js",
                'import("https://example.invalid/scratch.import.js");',
            ),
            ("javascripts/extra.js", 'fetch("https://example.invalid/scratch.fetch");'),
            (
                "javascripts/extra.mjs",
                'export default fetch("https://example.invalid/scratch.module");',
            ),
            # Material's own scripts are built into this directory; a file of
            # the author's there is still the author's.
            (
                "assets/javascripts/scratch.min.js",
                'fetch("https://example.invalid/scratch.material-dir");',
            ),
        ],
    )
    def test_built_script_loading_an_off_origin_resource_fails(
        self, tmp_path: Path, source: str, script: str
    ) -> None:
        repo = _scratch_repo(tmp_path)
        (repo / "docs" / source).parent.mkdir(parents=True, exist_ok=True)
        (repo / "docs" / source).write_text(f"{script}\n")
        with (repo / "mkdocs.yml").open("a") as config:
            config.write(f"\nextra_javascript:\n  - {source}\n")

        result = _run_docs_gate(repo)
        output = result.stdout + result.stderr

        assert result.returncode != 0, f"check-docs.sh accepted {script!r}"
        assert "built pages load an off-origin resource" in output, output
        assert "example.invalid/scratch." in output, output

    @pytest.mark.parametrize(
        "body",
        [
            '<image href="https://example.invalid/scratch.image.png"/>',
            '<IMAGE xlink:href="//example.invalid/scratch.xlink.png"/>',
            "<style>rect { fill: url(https://example.invalid/scratch.fill.svg#g) }"
            "</style>",
            "<rect style=\"fill:url('https://example.invalid/scratch.attr.svg#g')\"/>",
        ],
    )
    def test_built_svg_loading_an_off_origin_resource_fails(
        self, tmp_path: Path, body: str
    ) -> None:
        # An SVG opened as a document (`<object>`, `<embed>`, a direct visit)
        # fetches what it names, like a page.
        repo = _scratch_repo(tmp_path)
        (repo / "docs/assets/scratch.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" '
            'xmlns:xlink="http://www.w3.org/1999/xlink">'
            f"{body}</svg>\n"
        )
        with (repo / "docs/index.md").open("a") as page:
            page.write('\n<object data="assets/scratch.svg"></object>\n')

        result = _run_docs_gate(repo)
        output = result.stdout + result.stderr

        assert result.returncode != 0, f"check-docs.sh accepted {body!r}"
        assert "built pages load an off-origin resource" in output, output
        assert "scratch.svg" in output, output
        assert "example.invalid/scratch." in output, output

    def test_built_svg_declaring_an_entity_fails(self, tmp_path: Path) -> None:
        # An internal DTD entity spells the URL only once the browser expands
        # it, so the attribute scan reads `&u;`, never the URL.
        repo = _scratch_repo(tmp_path)
        (repo / "docs/assets/scratch.svg").write_text(
            '<!DOCTYPE svg [<!ENTITY u "https://example.invalid/scratch.e.png">]>\n'
            '<svg xmlns="http://www.w3.org/2000/svg"><image href="&u;"/></svg>\n'
        )

        result = _run_docs_gate(repo)
        output = result.stdout + result.stderr

        assert result.returncode != 0, output
        assert "built pages load an off-origin resource" in output, output
        assert "scratch.svg: <!ENTITY>" in output, output

    @pytest.mark.parametrize(
        ("source", "markup", "attribute"),
        [
            (
                "index.md",
                '<img src="assets/scratch.svg" onerror="fetch(\'https://example.'
                "invalid/scratch.onerror')\">",
                "onerror",
            ),
            # Refused whatever the handler holds: a URL can be assembled.
            ("index.md", '<img src="x.png" ONERROR="go()" alt="">', "onerror"),
            ("index.md", '<iframe srcdoc="&lt;p&gt;x&lt;/p&gt;"></iframe>', "srcdoc"),
            (
                "assets/scratch.svg",
                '<svg xmlns="http://www.w3.org/2000/svg" onload="go()"></svg>',
                "onload",
            ),
        ],
    )
    def test_built_event_handler_or_srcdoc_attribute_fails(
        self, tmp_path: Path, source: str, markup: str, attribute: str
    ) -> None:
        # The clean site carries no `on*` or `srcdoc` attribute: Material's
        # templates use none, so any one is an author's script.
        repo = _scratch_repo(tmp_path)
        with (repo / "docs" / source).open("a") as page:
            page.write(f"\n{markup}\n")

        result = _run_docs_gate(repo)
        output = result.stdout + result.stderr

        assert result.returncode != 0, f"check-docs.sh accepted {markup!r}"
        assert "built pages load an off-origin resource" in output, output
        assert attribute in output, output

    @pytest.mark.parametrize(
        ("setting", "key"),
        [
            (
                "repo_url: https://github.com/example/scratch\n"
                "repo_name: example/scratch\n",
                "repo_url",
            ),
            (
                "extra:\n  analytics:\n    provider: google\n    property: G-SCRATCH\n",
                "extra.analytics",
            ),
        ],
    )
    def test_mkdocs_setting_that_loads_an_off_origin_resource_fails(
        self, tmp_path: Path, setting: str, key: str
    ) -> None:
        # Refused in the config, not only in the built page: Material builds
        # the GitHub API and Google Tag Manager URLs in JS, never in a src.
        repo = _scratch_repo(tmp_path)
        with (repo / "mkdocs.yml").open("a") as config:
            config.write(f"\n{setting}")

        result = _run_docs_gate(repo)
        output = result.stdout + result.stderr

        assert result.returncode != 0, f"check-docs.sh accepted {setting!r}"
        assert f"mkdocs.yml sets {key}" in output, output

    def test_site_dir_moved_away_from_site_still_has_its_pages_checked(
        self, tmp_path: Path
    ) -> None:
        # The built-site stages read `site/`; a `site_dir` elsewhere left them
        # scanning an empty path and passing.
        repo = _scratch_repo(tmp_path)
        with (repo / "mkdocs.yml").open("a") as config:
            config.write("\nsite_dir: scratch-elsewhere\n")
        with (repo / "docs/index.md").open("a") as page:
            page.write(
                '\n<link rel="stylesheet" href="https://example.invalid/scratch.css">\n'
            )

        result = _run_docs_gate(repo)
        output = result.stdout + result.stderr

        assert result.returncode != 0, output
        assert "built pages load an off-origin resource" in output, output

    def test_navigational_links_and_anchors_to_another_origin_pass(
        self, tmp_path: Path
    ) -> None:
        # A hyperlink names a URL without the browser fetching it.
        repo = _scratch_repo(tmp_path)
        with (repo / "docs/index.md").open("a") as page:
            page.write(
                "\n"
                '<link rel="me" href="https://example.invalid/scratch.me">\n'
                '<link rel="author license" href="https://example.invalid/scratch.l">\n'
                '<link rel="help" href="https://example.invalid/scratch.help">\n'
                '<a href="https://github.com/thossullivan/kew">kew on GitHub</a>\n'
            )

        result = _run_docs_gate(repo)
        output = result.stdout + result.stderr

        assert result.returncode == 0, output
        assert "no off-origin resource" in output, output

    @pytest.mark.parametrize(
        "citation",
        [
            _AUDIT + "2026-03-25-codebase" + "-audit.md",
            "2026-03-25-codebase" + "-audit.md",
        ],
    )
    def test_archived_audit_pack_cited_by_site_path_or_basename_fails(
        self, tmp_path: Path, citation: str
    ) -> None:
        # #1993 (1): DELETED_TREES matches only the full `docs/audit/<pack>`
        # path, so the site-relative path and the bare basename slipped past.
        repo = _scratch_repo(tmp_path)
        with (repo / "docs/index.md").open("a") as page:
            page.write(f"\nSee `{citation}`.\n")

        result = _run_docs_gate(repo)
        output = result.stdout + result.stderr

        assert result.returncode != 0, f"check-docs.sh accepted {citation!r}"
        assert (
            "archived audit pack '2026-03-25-codebase" + "-audit.md' is still cited"
        ) in output, output
        assert "✓ no deleted internal tree returned" not in output, output

    def test_committed_epic_plan_manifest_passes_the_gate(self, tmp_path: Path) -> None:
        # #1993 (2): the manifest `kew epic plan --from-manifest` reads was
        # never archived; committing it must not read as a returned plan.
        repo = _scratch_repo(tmp_path)
        relative = ".kew/" + "epic-plan-manifest.json"
        manifest = repo / relative
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text('{"nodes": []}\n')
        subprocess.run(["git", "add", "--force", relative], cwd=repo, check=True)

        result = _run_docs_gate(repo)
        output = result.stdout + result.stderr

        assert result.returncode == 0, output
        assert "epic-plan-manifest.json is internal" not in output, output


# ---------------------------------------------------------------------------
# Node S8: Sandbox model documentation
# ---------------------------------------------------------------------------


class TestDocumentSandbox:
    def test_configuration_md_documents_all_sandbox_keys(self):
        text = _read("docs/configuration.md")
        for key in ("enabled", "runtime", "image", "network", "on_missing_runtime"):
            assert key in text, f"docs/configuration.md missing sandbox key: {key!r}"

    def test_configuration_md_has_sandbox_section(self):
        text = _read("docs/configuration.md")
        assert "[sandbox]" in text, (
            "docs/configuration.md must document the [sandbox] config table"
        )

    def test_security_md_states_guard_is_layer_2(self):
        text = _read("SECURITY.md")
        # Guard must be described honestly as best-effort / layer 2
        assert "layer 2" in text or "best-effort" in text, (
            "SECURITY.md must describe in-process guard as best-effort / layer 2"
        )

    def test_security_md_names_mount_boundary_as_primary_containment(self):
        text = _read("SECURITY.md")
        assert "mount boundary" in text, (
            "SECURITY.md must name the sandbox mount boundary as primary containment"
        )

    def test_security_md_references_known_bypass(self):
        text = _read("SECURITY.md")
        # Must be honest that the guard is known-bypassable
        assert "#502" in text or "bypassable" in text or "known bypass" in text, (
            "SECURITY.md must acknowledge the known in-process guard bypass (#502)"
        )

    def test_security_md_documents_credential_posture(self):
        text = _read("docs/security.md")
        # GitHub token must be host-only
        assert "host-only" in text or "host only" in text, (
            "docs/security.md must document GitHub token as host-only"
        )
        # The credential posture as the code ACTUALLY implements it (#1146).
        # This replaced a "short-TTL key" claim that no code ever backed: kew
        # mints nothing, it forwards a host env var BY NAME and inherits
        # whatever lifetime that key already has. A truthfulness test pinning
        # an aspiration is worse than no test — it certifies the wrong posture.
        assert "by name only" in text.lower(), (
            "docs/security.md must document that credentials travel by name only"
        )
        assert "per-runner" in text.lower(), (
            "docs/security.md must document the per-runner credential allowlist"
        )
        for name in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
            assert name in text, (
                f"docs/security.md must name {name} in the per-runner allowlist"
            )

    def test_how_it_works_md_describes_sandboxed_flow(self):
        text = _read("docs/how-it-works.md")
        assert "standalone checkout" in text or "Sandboxed" in text, (
            "docs/how-it-works.md must describe the sandboxed dispatch flow"
        )
        lower = text.lower()
        assert "host-brokered" in lower or "host brokered" in lower, (
            "docs/how-it-works.md must describe host-brokered push/PR"
        )


# ---------------------------------------------------------------------------
# Epic driver-tick polish: empty-graph error, post-action summary, id render
# ---------------------------------------------------------------------------


class TestDocumentEpicDriverPolish:
    def test_how_it_works_documents_empty_graph_error(self):
        text = _read("docs/how-it-works.md")
        lower = text.lower()
        # An empty tracker query must be documented as an error, not COMPLETE 0/0.
        assert "zero nodes" in lower, (
            "docs/how-it-works.md must document the empty-graph (zero nodes) case"
        )
        assert "done=0" in lower or "0/0" in lower, (
            "docs/how-it-works.md must contrast against the old COMPLETE done=0 output"
        )

    def test_how_it_works_documents_post_action_summary(self):
        text = _read("docs/how-it-works.md")
        assert "post-action" in text.lower(), (
            "docs/how-it-works.md must document the post-action tick summary "
            "(the tick's summary reflects its own merges/closes)"
        )

    def test_how_it_works_documents_tracker_aware_id_rendering(self):
        text = _read("docs/how-it-works.md")
        # The one id-render rule: bare for non-GitHub, #N for GitHub.
        assert "item_ref" in text, (
            "docs/how-it-works.md must name the item_ref id-render rule"
        )
        assert "bare" in text.lower(), (
            "docs/how-it-works.md must state non-GitHub ids render bare"
        )

    def test_epic_md_documents_every_operator_attention_event_kind(self):
        """#2150: the page promises one event per state that needs a person.

        Both halves are pinned — the kinds the page names, and the vocabulary
        the driver actually emits — so a kind added or renamed in one place
        cannot leave the other standing.
        """
        from kew.cli import epic_app
        from kew.graph.driver.attention import AttentionKind

        text = _read("docs/cli/epic.md")
        assert "kew epic watch" in text, (
            "docs/cli/epic.md must document the operator-attention stream"
        )
        for kind in AttentionKind:
            assert f"`{kind.value}`" in text, (
                f"docs/cli/epic.md must name the {kind.value} event kind"
            )
        assert "watch" in [c.name for c in epic_app.registered_commands], (
            "the documented `kew epic watch` command must actually be registered"
        )

    def test_epic_md_status_section_names_every_terminal_signal(self):
        """#2183: `kew epic status` can now report `awaiting_merges`, so the
        section that enumerates its signal cannot keep calling it a tri-state.

        Pinned against the enum so a fifth signal cannot be added while the
        command's own section still promises the old list.
        """
        from kew.graph.driver.result import TerminalSignal

        text = _read("docs/cli/epic.md")
        start = text.index("## `kew epic status`")
        section = text[start : text.index("\n## ", start)]
        assert "tri-state" not in section, (
            "the status section must not call a four-valued signal tri-state"
        )
        for signal in TerminalSignal:
            assert f"`{signal.value}`" in section, (
                f"docs/cli/epic.md's status section must name the {signal.value} "
                "signal it can print"
            )

    def test_epic_md_documents_empty_graph_exit_two(self):
        text = _read("docs/cli/epic.md")
        lower = text.lower()
        assert "zero nodes" in lower, (
            "docs/cli/epic.md must document the empty-graph (zero nodes) case"
        )
        assert (
            "exit 2" in lower or "exit **2**" in lower or "errors (exit 2)" in lower
        ), "docs/cli/epic.md must document the empty-graph exit-2 diagnostic"

    def test_epic_md_documents_retry_refusing_a_held_node(self, tmp_path):
        """#2154: retry reported success on a review-held node, then left it held.

        The page now promises a refusal that records nothing, so both halves are
        pinned: the sentence, and the verb actually raising before any effector.
        """
        from kew.config import DispatchConfig
        from kew.run_store import SqliteRunStore
        from kew.service import verbs

        text = _read("docs/cli/epic.md")
        assert "retry cannot clear a hold" in text, (
            "docs/cli/epic.md must document that retry refuses a held node"
        )
        assert "kew epic unhold <epic> <item>" in text, (
            "the refusal doc must name the command that does clear a hold"
        )
        # The refusal is a code-2 outcome in the scheme every exit claim binds to.
        row = next(
            line
            for line in _read("docs/cli/exit-codes.md").splitlines()
            if line.startswith("| `epic` |")
        )
        cells = [cell.strip() for cell in row.strip("|").split("|")][1:]
        assert "Retry refused on a held node" in cells[2].split("; ")

        config = DispatchConfig()
        config.project.repo = "o/r"
        with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
            store.hold_epic_node_item("epic:x", "42", "review_unparseable")
            with pytest.raises(verbs.EpicRetryHeld):
                verbs.epic_retry(store, epic_ref="epic:x", item_id="42", config=config)
            assert store.list_epic_retries("epic:x") == [], (
                "a refused retry must record no marker"
            )

    def test_epic_md_scopes_the_refusal_to_holds_the_driver_honors(self, tmp_path):
        """The page must not promise a refusal wider than the driver's reading.

        ``tick._hold_refused_frontier_node`` writes hold rows the overlay does
        not route to HELD; those nodes read FAILED and stay dispatchable, so
        retry is still the documented recovery path for them.
        """
        from unittest.mock import MagicMock, patch

        from kew.config import DispatchConfig
        from kew.run_store import SqliteRunStore
        from kew.service import verbs

        text = _read("docs/cli/epic.md")
        assert "still dispatchable, so retry" in text, (
            "docs/cli/epic.md must say retry still runs for an unrecognised hold"
        )

        config = DispatchConfig()
        config.project.repo = "o/r"
        with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
            store.hold_epic_node_item(
                "epic:x", "42", "credential_issuer_refused: unsupported route"
            )
            with patch("kew.labels.run") as gh:
                gh.return_value = MagicMock(returncode=0, stdout="", stderr="")
                verbs.epic_retry(store, epic_ref="epic:x", item_id="42", config=config)
            assert [r.item_id for r in store.list_epic_retries("epic:x")] == ["42"]

    def test_epic_md_names_exactly_the_hold_prefixes_the_overlay_routes_to_held(
        self,
    ):
        """The retry section says its refusal "matches exactly" the reasons the
        overlay routes to HELD, then lists their prefixes. The ``review_*``
        prefixes are the governed-review reasons it names as a group; every
        other prefix ``holds_node_out_of_frontier`` honors is listed by name,
        and the list names nothing else."""
        from kew.epic_holds import FRONTIER_HOLD_PREFIXES

        text = _read("docs/cli/epic.md")
        start = text.index('"Actually holding" is the driver\'s reading')
        paragraph = text[start : text.index("\n\n", start)]
        named = set(re.findall(r"`([^`]+)`", paragraph))
        group = {p for p in FRONTIER_HOLD_PREFIXES if p.startswith("review_")}
        assert "the governed-review reasons" in paragraph
        missing = set(FRONTIER_HOLD_PREFIXES) - group - named
        assert not missing, f"docs/cli/epic.md must list these prefixes: {missing}"
        extra = named - set(FRONTIER_HOLD_PREFIXES) - {"ci_pending"}
        assert not extra, f"docs/cli/epic.md lists prefixes kew does not hold: {extra}"


# ---------------------------------------------------------------------------
# No ghost runners: every runner name in docs must exist in runners/registry.py
# ---------------------------------------------------------------------------


def _valid_runner_names() -> set[str]:
    from kew.runners.registry import _get_runner_factories

    return set(_get_runner_factories())


def _runner_names_in_text(text: str) -> set[str]:
    """Extract candidate runner name tokens from a text.

    Matches:
    - **`runner-name`** (runner table in README)
    - runner = "runner-name" (TOML routing examples, commented or not)
    - [agent.runner-name] (TOML section headers and inline references)
    """
    candidates: set[str] = set()
    candidates |= set(re.findall(r"\*\*`([^`]+)`\*\*", text))
    candidates |= set(re.findall(r'runner\s*=\s*"([^"]+)"', text))
    candidates |= set(re.findall(r"\[agent\.([^\]]+)\]", text))
    # Keep only plain lowercase identifiers (exclude model patterns like claude-*, etc.)
    return {s for s in candidates if re.match(r"^[a-z][a-z0-9-]+$", s)}


class TestNoGhostRunners:
    """Assert no runner name appears in docs that is absent from runners/registry.py."""

    def test_no_ghost_runners_in_root_readme(self) -> None:
        valid = _valid_runner_names()
        found = _runner_names_in_text(_read("README.md"))
        ghosts = found - valid
        assert not ghosts, f"Ghost runner(s) in README.md not in registry: {ghosts}"

    def test_no_ghost_runners_in_create_kew_template(self) -> None:
        valid = _valid_runner_names()
        found = _runner_names_in_text(_read("create-kew/templates/kew.toml"))
        ghosts = found - valid
        assert not ghosts, (
            f"Ghost runner(s) in create-kew template not in registry: {ghosts}"
        )


# Claim shapes that tell an operator a Codex dispatch runs on the host, or that
# kew holds no container boundary for it. Every one of them reads as
# runtime-independent, and every one of them WAS, until the gateway lane started
# containing Codex itself (PR #2461). Matched within one sentence — `[^.|]*`
# stops at a sentence end and at a table-cell wall — so a neighbouring sentence
# can neither trip a pattern nor satisfy it.
_CODEX_HOST_CLAIMS = (
    r"codex[^.|]*\b(?:stays|runs|executes|continues|remains|uses)\b"
    r"[^.|]*\bon the (?:trusted )?host\b",
    r"codex[^.|]*\bhost path\b",
    r"codex[^.|]*\bsandboxes itself\b",
    r"codex[^.|]*\blaunches no container\b",
    r"codex[^.|]*\b(?:does not need|needs no|need none of)\b"
    r"[^.|]*\b(?:container|runtime|image)\b",
    r"codex[^.|]*\bneither required nor used\b",
    r"codex[^.|]*\bnot placed in\b[^.|]*\bcontainer\b",
    r"codex[^.|]*\bexternal containment is refused\b",
    r"codex[^.|]*\boutside this container contract\b",
    r"codex[^.|]*\bdeclares `native`",
    r"codex[^.|]*\bcontainer posture as disabled\b",
)

# What makes such a claim honest: naming `openshell`, the one runtime that
# answers differently, or saying the claim holds on every runtime — which is
# true of the lanes where kew offers no boundary anywhere (a first-class loop,
# a preserved continuation).
_CODEX_CLAIM_SCOPES = ("openshell", "every runtime", "any runtime", "each runtime")

# Or limiting the claim, in the same sentence, to the runtimes where it is
# true: "on `docker` and `podman`", "on `runtime = "docker"`", "on `[sandbox]
# runtime = "docker"`". A bare mention of docker elsewhere in the sentence is
# not a scope; the preposition that restricts the claim is.
_CODEX_RUNTIME_SCOPE = re.compile(
    r"\bon (?:the )?`?(?:\[sandbox\] )?(?:runtime = )?\"?(?:docker|podman)\b"
)


def _names_its_scope(claim: str) -> bool:
    """Whether one claim block says which runtimes it holds on."""
    return any(scope in claim for scope in _CODEX_CLAIM_SCOPES) or bool(
        _CODEX_RUNTIME_SCOPE.search(claim)
    )


_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _list_items(para: str) -> Iterator[str]:
    """*para* cut at each list marker; a paragraph with none is one item."""
    item: list[str] = []
    for line in para.splitlines():
        if _LIST_ITEM.match(line) and item:
            yield "\n".join(item)
            item = []
        item.append(line)
    if item:
        yield "\n".join(item)


def _claim_blocks(text: str) -> Iterator[str]:
    """One sentence, or one table ROW, normalised to a single line.

    A row is its own block on purpose: a reader scans the row for `codex` and
    reads that cell, so a cell cannot lean on a sibling row's qualification.
    Inside a row `|` becomes a separator the patterns cross freely; between
    rows they never meet. A sentence, and so a list item, is its own block for
    the same reason (#2527): a flat sentence cannot borrow the scope a sibling
    sentence or a sibling item names.
    """
    for para in text.split("\n\n"):
        rows = [line for line in para.splitlines() if line.lstrip().startswith("|")]
        if rows:
            yield from (row.replace("|", " ; ") for row in rows)
            continue
        for item in _list_items(para):
            yield from _SENTENCE_END.split(" ".join(item.split()))


def _codex_host_claims(text: str) -> list[str]:
    """Every claim block in *text* that says Codex stays on the host."""
    found = []
    for block in _claim_blocks(text):
        one_line = " ".join(block.lower().split())
        if any(re.search(claim, one_line) for claim in _CODEX_HOST_CLAIMS):
            found.append(one_line)
    return found


def _unscoped_codex_claims(text: str) -> list[str]:
    """The Codex host claims in *text* that do not name their own scope."""
    return [claim for claim in _codex_host_claims(text) if not _names_its_scope(claim)]


def _codex_host_claim_blocks() -> list[tuple[str, str]]:
    """Every (page, block) under `docs/` that claims Codex stays on the host."""
    found = []
    for page in sorted((REPO_ROOT / "docs").rglob("*.md")):
        rel = page.relative_to(REPO_ROOT).as_posix()
        found.extend((rel, claim) for claim in _codex_host_claims(page.read_text()))
    return found


class TestCodexRunnerReference:
    PAGE = "docs/runners/codex.md"

    def test_runner_index_represents_every_registered_runner(self) -> None:
        text = _read("docs/runners/index.md")
        for runner in _valid_runner_names():
            assert f"`{runner}`" in text

    def test_security_capability_table_equals_runner_code(self) -> None:
        import kew.runners.protocol as protocol
        from kew.runners.registry import _get_runner_factories

        render = getattr(protocol, "render_security_capability_table", None)
        assert callable(render), "runner protocol must expose security capabilities"
        runners = [factory() for factory in _get_runner_factories().values()]
        generated = render(runners)

        assert generated in _read("docs/runners/index.md")

    def test_codex_page_and_nav_exist(self) -> None:
        assert (REPO_ROOT / self.PAGE).is_file()
        assert "runners/codex.md" in _mkdocs_nav_block()

    def test_codex_reference_pins_runtime_and_security_truth(self) -> None:
        text = _read(self.PAGE)
        for token in (
            'runner = "codex"',
            'model = "',
            "reasoning_effort",
            "service_tier",
            "0.145.0",
            "codex login",
            "codex login status",
            "workspace-write",
            "publication_style",
            "API-equivalent included-plan value",
            ".codex/rules/",
            ".codex/skills/",
            ".codex/config.toml",
            "OPENAI_API_KEY",
            "CODEX_ACCESS_TOKEN",
            "required_credentials == ()",
        ):
            assert token in text

    def test_codex_reference_pins_supported_and_refused_flows(self) -> None:
        text = _read(self.PAGE).lower()
        for phrase in (
            "standard issue dispatch",
            "first-class loops",
            "conflict resolution",
            "standalone `kew review`",
            "opencode",
            "external container",
        ):
            assert phrase in text
        assert "codex container image" not in text
        assert "mount personal `.codex`" not in text

    def test_codex_adversarial_refusal_names_the_guard_that_fires(self) -> None:
        """The reviewer's Codex refusal is a credential guard, not a gateway one.

        This page briefly blamed the gateway — "on `openshell` a command-line
        launch has no gateway path" (PR #2461 review). That refusal
        (``kew.openshell_launch.ARGV_LAUNCH_REFUSAL``) belongs to the flows that
        swap ONE execution step for a container argv — ``kew review``, ``kew
        resolve``, ``kew ci-fix``, an epic's plan run, the callers that pass
        ``gateway_launch=False``. The production adversarial reviewer hands its
        WHOLE launch to ``run_container_and_stream``, which is the one dispatch
        shape the gateway does serve, so it never passes that flag. What
        refuses Codex there is the launcher's own
        ``runner_required_credentials`` guard, ahead of runtime detection and
        the same on every runtime (``test_review_stage_production_wiring.py::
        test_mapped_codex_reviewer_refuses_before_sidecar_or_container``).
        """
        from kew.graph.driver import review_launcher

        launcher = source_ast.module_tree(review_launcher)
        for call in source_ast.calls(launcher):
            flag = source_ast.keyword(call, "gateway_launch")
            assert not (isinstance(flag, ast.Constant) and flag.value is False), (
                f"review_launcher.py:{call.lineno} passes gateway_launch=False"
            )
        guards = [
            node
            for node in ast.walk(launcher)
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.UnaryOp)
            and isinstance(node.test.op, ast.Not)
            and isinstance(node.test.operand, ast.Call)
            and source_ast.callee(node.test.operand) == "runner_required_credentials"
        ]
        assert guards, "the reviewer no longer refuses on runner_required_credentials"

        parts = _read(self.PAGE).split("**Production adversarial review:**", 1)
        assert len(parts) == 2, f"{self.PAGE} lost its adversarial review clause"
        clause = parts[1].split("\n- **", 1)[0].split("\n## ", 1)[0]
        assert "required_credentials" in clause
        assert "every runtime" in clause
        for blamed in ("gateway path", "command line"):
            assert blamed not in clause, (
                f"{self.PAGE} blames {blamed!r} for a refusal the reviewer's "
                "credential guard makes before any launch shape is consulted"
            )

    @pytest.mark.policy
    def test_no_page_claims_codex_stays_on_the_host_unconditionally(self) -> None:
        """The `native` claim is per runtime now, so NO block may state it flat.

        `docs/runners/index.md` said Codex "stays on the host ... mapping an
        image is neither required nor used", full stop — false on
        `runtime = "openshell"`, where an unmapped Codex dispatch is refused
        `image_unmapped` (PR #2461 review round 3). The first version of this
        guard read three pages and accepted a page where ANY paragraph named
        the runtime, so the flat sentence survived in the page's other
        paragraphs and on every page the list left out — `configuration.md`
        ("It does not need a container runtime or image"), `how-it-works.md`
        ("kew records external container posture as disabled"), and
        `sandbox-entrypoint-contract.md`, whose "mapping an image cannot bypass
        its empty credential declaration" was the inverse of what this PR
        shipped (PR #2461 review round 4).

        So: every block under `docs/` that makes the claim, on any page, has to
        carry its own qualification. The posture itself is read back out of the
        runner rather than trusted to either text.
        """
        from kew.runners.registry import runner_containment_posture

        assert runner_containment_posture("codex") == "native"
        assert runner_containment_posture("codex", runtime="docker") == "native"
        assert runner_containment_posture("codex", runtime="openshell") is None

        claims = _codex_host_claim_blocks()
        unscoped = [
            f"{rel}: {block[:160]}"
            for rel, block in claims
            if not _names_its_scope(block)
        ]
        assert not unscoped, (
            "Codex posture stated without naming the runtime that changes it "
            f"(one of {_CODEX_CLAIM_SCOPES}, or on docker/podman):\n"
            + "\n".join(unscoped)
        )

        pages = {rel for rel, _ in claims}
        for rel in (
            "docs/configuration.md",
            "docs/how-it-works.md",
            "docs/runners/index.md",
            "docs/sandbox-entrypoint-contract.md",
            "docs/security.md",
            self.PAGE,
        ):
            assert rel in pages, (
                f"{rel} no longer states where a Codex dispatch runs in a shape "
                "this guard recognises — the scan is only honest while it "
                "reaches the pages that make the claim"
            )

    @pytest.mark.policy
    def test_codex_posture_claims_name_their_scope_in_the_same_claim(
        self,
    ) -> None:
        """A flat host claim cannot borrow its scope from a neighbour (#2527).

        Table rows were already checked one at a time, but a paragraph or a
        list was one block, so a sentence saying Codex runs on the host passed
        whenever ANOTHER sentence in the same paragraph, or another item in
        the same list, named `openshell`. A reader who stops at the flat
        sentence reads it as true on every runtime. Each sentence and each list
        item is its own claim now, and every shipped page still passes.
        """
        leaning_sentence = (
            "Codex runs on the host. On `openshell` kew holds the boundary instead.\n"
        )
        leaning_item = (
            "- Codex runs on the host and launches no container.\n"
            "- Under `openshell` the gateway sandbox holds the boundary.\n"
        )
        bare_mention = "Unlike a docker image, Codex runs on the host.\n"
        for text in (leaning_sentence, leaning_item, bare_mention):
            assert _unscoped_codex_claims(text), (
                f"the posture guard accepted a flat host claim that leans on "
                f"its neighbour for scope: {text!r}"
            )

        scoped = (
            "On `docker`, Codex runs on the host; under `openshell` kew "
            "holds the boundary.\n\n"
            "- On every runtime, a preserved continuation of Codex runs on "
            "the host.\n"
            '- On `runtime = "podman"` Codex runs on the host.\n'
        )
        assert _unscoped_codex_claims(scoped) == []

        unscoped = [
            f"{rel}: {claim[:160]}"
            for rel, claim in _codex_host_claim_blocks()
            if not _names_its_scope(claim)
        ]
        assert not unscoped, "\n".join(unscoped)

    def test_the_codex_posture_table_names_every_host_case_on_the_gateway(
        self,
    ) -> None:
        """One `sandboxed` row; a row for each way the boundary is not on offer.

        The table is the only place an operator can read which runs on the
        gateway runtime stay on the host, and two of them arrived after review
        round 3: a host with no gateway CLI installed (only under
        `dangerously_skip_containment` since #2623), and a preserved
        continuation. Each is an arm of `kew.sandbox._offered_runtime`, and a
        missing row is a silent posture surprise.
        """
        table = _read(self.PAGE).split("### Posture per runtime", 1)[1]
        rows = [
            row
            for row in table.splitlines()
            if row.startswith("|") and ("`native`" in row or "`sandboxed`" in row)
        ]
        contained = [row for row in rows if "`sandboxed`" in row]
        assert len(contained) == 1 and "openshell" in contained[0], rows
        for case in (
            "docker",
            "the gateway cannot launch",
            "no gateway CLI",
            "preserved checkout",
        ):
            host_rows = [row for row in rows if case in row and "`native`" in row]
            assert len(host_rows) == 1, (case, rows)

    def test_entrypoint_contract_states_the_codex_lane_it_once_called_impossible(
        self,
    ) -> None:
        """The container contract page denied the lane this PR shipped.

        It told image authors that for Codex "mapping an image cannot bypass
        its empty credential declaration" — a security claim, read by whoever
        builds an image — while `_require_containable_runner` admits exactly
        that mapping on the gateway runtime (PR #2461 review round 4). Anchored
        on the admission, not on the sentence: the page is where the meaning of
        an empty declaration is documented, so the two cannot be allowed to
        drift apart again.
        """
        from kew.config import SandboxConfig
        from kew.runners.registry import runner_required_credentials
        from kew.sandbox import SandboxCredentialUndeclared, _require_containable_runner

        assert runner_required_credentials("codex") == ()
        image = "kew-agent-codex:test"
        gateway = SandboxConfig(runtime="openshell", images={"codex": image})
        assert (
            _require_containable_runner(gateway, "codex", runtime="openshell") == image
        )
        docker = SandboxConfig(runtime="docker", images={"codex": image})
        with pytest.raises(SandboxCredentialUndeclared):
            _require_containable_runner(docker, "codex", runtime="docker")

        page = _read("docs/sandbox-entrypoint-contract.md")
        clause = page.split("Official Codex is deliberately outside", 1)
        assert len(clause) == 2, "the entrypoint contract lost its Codex section"
        section = clause[1].split("\nConsequences an image", 1)[0]
        assert "cannot bypass" not in section, (
            "the entrypoint contract denies a mapped Codex image the gateway "
            "runtime admits"
        )
        assert "openshell" in section and "docker" in section, section

    def test_reference_docs_use_current_agent_sections(self) -> None:
        from pydantic import BaseModel

        from kew.config import AgentConfig

        expected = {
            field.alias or name
            for name, field in AgentConfig.model_fields.items()
            if isinstance(field.annotation, type)
            and issubclass(field.annotation, BaseModel)
        }
        text = _read("docs/configuration.md")
        for runner in expected:
            assert f"[agent.{runner}]" in text

    def test_codex_security_facts_span_reference_pages(self) -> None:
        combined = "\n".join(
            _read(path)
            for path in (
                "docs/configuration.md",
                "docs/authentication.md",
                "docs/security.md",
                "SECURITY.md",
                "docs/threat-model.md",
                "docs/sandbox-entrypoint-contract.md",
            )
        )
        for token in (
            "Codex-native",
            "official CLI login",
            "host broker",
            "workspace-write",
            "credential declaration is empty",
        ):
            assert token.lower() in combined.lower()


class TestPublicRunnerCatalog:
    def test_readme_and_website_match_registry(self) -> None:
        runners = _valid_runner_names()
        readme = _read("README.md")
        for runner in runners:
            assert f"`{runner}`" in readme
        source = _read("www/src/lib/data/runners.ts")
        block = source.split("export const RUNNERS", 1)[1].split(
            "export const INVARIANTS", 1
        )[0]
        ids = set(re.findall(r"\bid:\s*'([^']+)'", block))
        assert ids == runners

    def test_lemonade_is_flagged_experimental_and_the_flag_renders(self) -> None:
        # Ruling 3 (2026-08-24): Lemonade is experimental everywhere the public
        # sees it. The data flag alone is inert (found on #1720 round 1); the
        # component must read it.
        source = _read("www/src/lib/data/runners.ts")
        block = source.split("export const RUNNERS", 1)[1]
        lemonade = block.split("id: 'lemonade'", 1)[1].split("\n  },", 1)[0]
        assert "experimental: true" in lemonade
        # The install section's runner row names every runner and marks it.
        component = _read("www/src/lib/Install.svelte")
        assert "import { RUNNERS } from './data/runners';" in component
        assert "r.experimental" in component
        assert '<span class="tag">&nbsp;&middot; experimental</span>' in component

    def test_canary_page_pins_codex_lifecycle_truth(self) -> None:
        text = _read("docs/cli/canary.md")
        for token in (
            "--model",
            "--exercise-resume",
            "session deletion",
            "tree clean",
            "drift",
            "incomplete",
            "success",
            "raw",
            "sensitive",
        ):
            assert token.lower() in text.lower()

    def test_readme_uses_canonical_docs_origin(self) -> None:
        text = _read("README.md")
        assert "https://thossullivan.github.io/kew" not in text
        assert "https://kew.dev/docs/" in text


class TestCodexCommandSurfaceTruth:
    def test_getting_started_and_run_are_runner_neutral(self) -> None:
        getting_started = _read("docs/getting-started.md").lower()
        assert "official codex" in getting_started
        assert "codex login" in getting_started
        assert "configured runner" in _read("docs/cli/run.md").lower()
        assert "configured runner" in _read("docs/cli/index.md").lower()

    def test_host_publication_brokers_are_documented(self) -> None:
        review = _read("docs/cli/review.md").lower()
        resolve = _read("docs/cli/resolve.md").lower()
        assert "host-publication" in review
        assert "host-publication" in resolve
        assert "kew commits" in review
        assert "kew commits" in resolve
        # Agent checkouts are standalone clones, never linked worktrees; the
        # docs say "checkout" and a failed publication must keep it on disk.
        assert "preserves the checkout" in review
        assert "preserves the checkout" in resolve

    def test_audit_and_opencode_distinguish_official_codex(self) -> None:
        assert "openai:via-codex" in _read("docs/cli/audit.md")
        opencode = _read("docs/runners/opencode.md").lower()
        assert "official `codex` runner" in opencode
        assert "gpt/o-series" in opencode

    def test_hermes_uses_current_agent_section(self) -> None:
        text = _read("docs/runners/hermes.md")
        assert "[agent.hermes]" in text
        assert "\n[hermes]" not in text


class TestTrustPageRuntimeTruth:
    def test_security_documents_canonical_loops(self) -> None:
        text = " ".join(_read("docs/security.md").split())
        assert "First-class loops publish canonically by default (#1844)." in text
        assert "do not yet publish canonically" not in text

    def test_loop_documents_polled_pause_and_cumulative_budget(self) -> None:
        text = " ".join(_read("docs/cli/loop.md").lower().split())
        for phrase in (
            "polls the pause flag at most once every three seconds",
            "refuses both new dispatches and resumes",
            "`paused` | yes",
            "prior cumulative spend",
            "step and wall-clock caps apply to each continuation session",
        ):
            assert phrase in text
        for stale_claim in (
            "cooperative pause",
            "between iterations",
            "does **not** interrupt",
            "fresh per-session allowance",
            "fresh allowance",
        ):
            assert stale_claim not in text

    def test_review_documents_both_publication_modes(self) -> None:
        text = " ".join(_read("docs/cli/review.md").lower().split())
        for phrase in (
            "canonical mode always uses host publication",
            "checkout mode has one agent-publication path",
            "attached, uncontained checkout",
        ):
            assert phrase in text
        assert (
            "in canonical mode, host git uses a fresh canonical repository" in text
        ), "review.md must qualify fresh repository use by publication mode"
        assert (
            "does not establish that the runner lacks github mutation authority" in text
        ), "review.md must not equate host publication with absent GitHub authority"
        assert "`review.md.j2`" in text and "`review.auto_reply`" in text, (
            "review.md must identify the source of comment-reply instructions"
        )
        assert "no commit, push, or github mutation authority" not in text

    def test_review_rejects_unqualified_repository_claim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        text = " ".join(_read("docs/cli/review.md").split())
        qualified = "In canonical mode, host Git uses a fresh canonical repository."
        assert text.count(qualified) == 1
        mutated = text.replace(qualified, "Host Git uses a fresh canonical repository.")
        for phrase in (
            "canonical mode always uses host publication",
            "checkout mode has one agent-publication path",
            "attached, uncontained checkout",
        ):
            assert phrase in mutated.lower()
        monkeypatch.setattr(f"{__name__}._read", lambda _: mutated)
        with pytest.raises(AssertionError, match="must qualify fresh repository use"):
            self.test_review_documents_both_publication_modes()

    def test_resolve_documents_both_publication_modes(self) -> None:
        text = " ".join(_read("docs/cli/resolve.md").lower().split())
        for phrase in (
            "canonical mode always uses host publication",
            "checkout mode has one agent-publication path",
            "uncontained agent-style runner",
            "canonical publication always creates a merge commit",
        ):
            assert phrase in text

    def test_operations_documents_standalone_planning_and_safe_reuse(self) -> None:
        text = " ".join(_read("docs/operations.md").lower().split())
        for phrase in (
            "`kew epic plan` creates a standalone clone",
            "`--no-hardlinks`",
            "resume refuses linked worktrees and unsafe standalone clones before reuse",
        ):
            assert phrase in text
        assert "without checking for linkage" not in text

    def test_audit_documents_posture_resolution_by_flow(self) -> None:
        text = " ".join(_read("docs/audit.md").lower().split())
        for phrase in (
            "`kew ci-fix` and `kew epic plan` use ordinary dispatch resolution",
            "resolve and attest their posture",
            "first-class loops remain separate",
        ):
            assert phrase in text

    def test_configuration_documents_unmapped_runner_skip_behavior(self) -> None:
        text = " ".join(_read("docs/configuration.md").lower().split())
        for phrase in (
            "the skip flag cannot override a missing image",
            "runtime detection succeeds",
            "the dispatch still refuses",
        ):
            assert phrase in text

    def test_tuning_documents_one_review_pool_under_two_caps(self) -> None:
        """Both review caps read one accumulator (#2153 review).

        `check_review_budget` sums `adversarial_review` *and*
        `review_remediation` rows whichever cap it is handed, so a page that
        sells the two caps as two pools mis-sizes `max_review_usd` — the very
        key a reviewer-pool `review_budget` hold tells the operator to raise.
        """
        from datetime import UTC, datetime

        from kew.budget import BudgetExceeded, check_review_budget
        from kew.run_store import (
            ADVERSARIAL_REVIEW_RUN_KIND,
            REVIEW_REMEDIATION_RUN_KIND,
            RunRecord,
            SqliteRunStore,
        )

        text = " ".join(_read("docs/tuning.md").lower().split())
        for phrase in (
            "there is one pool per node",
            "`adversarial_review` and `review_remediation` rows",
            "two caps over one pool, not two pools",
            "it reads the same pool `max_review_usd` does",
        ):
            assert phrase in text, phrase
        for stale_claim in (
            "the fix cycle draws from its own pool",
            "the per-node remediation pool",
            "one pool until #993 split them",
        ):
            assert stale_claim not in text, stale_claim

        # Drive the code the page describes: remediation spend alone exhausts
        # the reviewer cap, and both review kinds feed the one pool.
        now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
        with SqliteRunStore(":memory:") as store:
            for index, kind in enumerate(
                (REVIEW_REMEDIATION_RUN_KIND, ADVERSARIAL_REVIEW_RUN_KIND)
            ):
                store.save(
                    RunRecord(
                        run_id=f"r-{index}",
                        issue_num=42,
                        branch="agent/issue-42",
                        status="success",
                        exit_code=0,
                        started_at=now,
                        estimated_cost_usd=6.0,
                        item_id="42",
                        run_kind=kind,
                    )
                )
            with pytest.raises(BudgetExceeded) as refusal:
                check_review_budget(store, "42", max_review_usd=10.0, now=now)
            assert refusal.value.spent_usd == pytest.approx(12.0)

    @pytest.mark.policy
    def test_tuning_does_not_claim_a_retry_refills_the_review_pool(self) -> None:
        """Neither review gate is cycle-scoped (#2169 review).

        ``check_review_budget`` accepts a ``since`` cycle bound, but
        ``dispatch_reviewer``'s ``cycle_start`` is never passed by any caller —
        so both caps sum the node's whole review history and `kew epic retry`
        buys no fresh allowance. A page that promises one sends the operator to
        `kew epic retry` instead of to the cap the hold names.
        """
        from datetime import UTC, datetime

        from kew.budget import BudgetExceeded, check_review_budget
        from kew.run_store import (
            ADVERSARIAL_REVIEW_RUN_KIND,
            RunRecord,
            SqliteRunStore,
        )

        text = " ".join(_read("docs/tuning.md").lower().split())
        for phrase in (
            "neither cap is cycle-scoped",
            "no dispatch path passes one",
            "on its pre-retry total",
        ):
            assert phrase in text, phrase
        for stale_claim in (
            "counts only the current review cycle",
            "grants a fresh allowance",
            "grants a fresh review allowance",
        ):
            assert stale_claim not in text, stale_claim

        # The claim's code half: no call site anywhere in src/ hands a cycle
        # bound to the reviewer gate, so the parameter stays dead and every gate
        # sums the whole history. Flip that and this test fails with the page.
        passers = sorted(
            path.relative_to(REPO_ROOT / "kew/src").as_posix()
            for path in (REPO_ROOT / "kew/src").rglob("*.py")
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Call)
            and any(kw.arg == "cycle_start" for kw in node.keywords)
        )
        assert passers == [], passers

        # And the behaviour: a retried node is refused on its pre-retry spend.
        started = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
        with SqliteRunStore(":memory:") as store:
            store.save(
                RunRecord(
                    run_id="pre-retry",
                    issue_num=42,
                    branch="agent/issue-42",
                    status="success",
                    exit_code=0,
                    started_at=started,
                    estimated_cost_usd=12.0,
                    item_id="42",
                    run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
                )
            )
            store.mark_epic_retry_item("epic:x", "42")
            # The cycle bound exists and the round counter honours it...
            assert store.review_cycle_start("42") is not None
            assert store.review_round_count("42") == 0
            # ...while the pool does not: the pre-retry spend still refuses.
            with pytest.raises(BudgetExceeded) as refusal:
                check_review_budget(store, "42", max_review_usd=10.0)
            assert refusal.value.spent_usd == pytest.approx(12.0)

    def test_configuration_documents_the_shared_review_pool(self) -> None:
        # The block is a commented kew.toml sample, so each assertion has to
        # fit inside one comment line.
        text = " ".join(_read("docs/configuration.md").lower().split())
        for phrase in (
            "against all of the item's review-stage",
            "verdict rounds and governed remediations",
            "fix cycle costs far more than one reviewer pass",
        ):
            assert phrase in text, phrase

    def test_tuning_distinguishes_direct_and_sidecar_credentials(self) -> None:
        text = " ".join(_read("docs/tuning.md").lower().split())
        for phrase in (
            "direct container authentication",
            "driver's environment",
            "selected sidecar authentication",
            "worker credential",
        ):
            assert phrase in text


# ---------------------------------------------------------------------------
# Node N1 (#1143): the container entrypoint contract page
#
# The page is a *versioned interface*, so it must not be able to drift from the
# code it specifies: every constant it quotes is asserted against the real
# symbol, and the page itself must stay reachable (nav + inbound links).
# ---------------------------------------------------------------------------

_CONTRACT_DOC = "docs/sandbox-entrypoint-contract.md"


def _launcher_section() -> str:
    """The contract page's `kew-openshell-launch` section, as raw text.

    Sliced so the launcher assertions cannot be satisfied by a mention
    elsewhere on the page: the whole point of the section is that it is the one
    account of what the launcher does per runner.
    """
    text = _read(_CONTRACT_DOC)
    start = text.index("### When the runtime replaces the entrypoint")
    end = text.index("\n## ", start)
    return text[start:end]


def _launcher_case_branches() -> list[str]:
    """The program names `docker/kew-openshell-launch` branches on."""
    launcher = _read("docker/kew-openshell-launch")
    block = launcher.split('case "${AGENT_PROGRAM##*/}" in', 1)[1]
    return re.findall(r"^  ([a-z][a-z0-9-]*)\)$", block.split("\nesac", 1)[0], re.M)


def _dockerfiles_installing_the_launcher() -> dict[str, bool]:
    """Agent Dockerfile name → whether it also installs an agent entrypoint.

    An image that installs the launcher and no entrypoint has the launcher as
    its ONLY launch path, which is the distinction the page has to carry.
    """
    installers = {}
    for path in sorted((REPO_ROOT / "docker").glob("*.Dockerfile")):
        text = path.read_text()
        if "docker/kew-openshell-launch /usr/local/bin/" not in text:
            continue
        installers[path.name] = "/usr/local/bin/kew-agent-entrypoint" in text
    return installers


def _mkdocs_nav_block() -> str:
    """The `nav:` block of mkdocs.yml, as raw text.

    Sliced textually rather than parsed: mkdocs.yml carries MkDocs-specific YAML
    tags, and this file's convention is string-level assertions.
    """
    text = _read("mkdocs.yml")
    start = text.index("\nnav:\n") + 1
    rest = text[start + len("nav:\n") :]
    end = re.search(r"^\S", rest, re.MULTILINE)
    return rest[: end.start()] if end else rest


class TestSandboxEntrypointContractDoc:
    """The doc exists, is reachable, and quotes the code accurately."""

    def test_page_exists(self) -> None:
        assert (REPO_ROOT / _CONTRACT_DOC).is_file(), (
            f"{_CONTRACT_DOC} must exist — it is the host-side container contract"
        )

    def test_page_is_in_mkdocs_nav(self) -> None:
        assert "sandbox-entrypoint-contract.md" in _mkdocs_nav_block(), (
            f"{_CONTRACT_DOC} must be wired into the mkdocs.yml nav"
        )

    def test_page_is_linked_from_how_it_works(self) -> None:
        assert "sandbox-entrypoint-contract.md" in _read("docs/how-it-works.md"), (
            "docs/how-it-works.md must link the container entrypoint contract"
        )

    def test_page_is_linked_from_runners_index(self) -> None:
        assert "sandbox-entrypoint-contract.md" in _read("docs/runners/index.md"), (
            "docs/runners/index.md must link the container entrypoint contract"
        )

    def test_quotes_the_real_prompt_constants(self) -> None:
        from kew.sandbox import PROMPT_FILENAME, PROMPT_MOUNT_PATH

        text = _read(_CONTRACT_DOC)
        for const in (PROMPT_FILENAME, PROMPT_MOUNT_PATH):
            assert const in text, (
                f"{_CONTRACT_DOC} must quote kew.sandbox's {const!r} verbatim"
            )

    def test_every_docker_path_it_names_exists(self) -> None:
        """A deleted build input must not survive as a doc claim.

        The page kept asserting that "the Codex reference entrypoint defines
        prompt handoff and acknowledgement behavior" after
        docker/agent-codex-entrypoint.sh was deleted with the arrival of the
        OpenShell-only codex image (#2317 review).
        """
        text = _read(_CONTRACT_DOC)
        # Placeholders (`policy-<runner>.yaml`) name a shape, not a file.
        referenced = {
            span
            for span in re.findall(r"`([^`\n]+)`", text)
            if span.startswith("docker/") and "<" not in span
        }
        assert referenced, f"{_CONTRACT_DOC} must cite its build inputs by path"
        missing = sorted(path for path in referenced if not (REPO_ROOT / path).exists())
        assert not missing, (
            f"{_CONTRACT_DOC} names build inputs that do not exist: {missing}"
        )

    def test_the_codex_claim_matches_the_absent_codex_entrypoint(self) -> None:
        """The codex image's pre-dispatch work is the launcher's, not an
        entrypoint's — so the page must point at the launcher (#2317 review)."""
        text = _read(_CONTRACT_DOC)

        assert not (REPO_ROOT / "docker/agent-codex-entrypoint.sh").exists()
        assert "The Codex image ships no entrypoint of its own." in text
        assert "docker/kew-openshell-launch" in text
        assert "Codex reference entrypoint" not in text

    def test_documents_every_per_runner_launcher_branch(self) -> None:
        """The launcher's branch set is a code fact, so the page's enumeration
        of it is derived from the script rather than kept in step by hand.

        The section listed only `claude` and `opencode` after the codex branch
        landed — the one branch with no entrypoint to mirror, so the page was
        the only place its placeholder auth.json could have been described, and
        it did not mention the file at all (#2317 review).
        """
        branches = _launcher_case_branches()
        assert sorted(branches) == ["claude", "codex", "opencode"], branches

        section = _launcher_section()
        for program in branches:
            assert f"- `{program}` —" in section, (
                f"{_CONTRACT_DOC} must document the launcher's {program!r} "
                "pre-dispatch branch where it enumerates the branches"
            )
        assert "$HOME/.codex/auth.json" in section, (
            f"{_CONTRACT_DOC} must name the credential file the codex branch "
            "writes — the launcher is the only thing that writes it"
        )

    @pytest.mark.policy
    def test_describes_the_launcher_only_image_as_its_only_launch_path(
        self,
    ) -> None:
        """For an image that installs the launcher and no entrypoint, the
        launcher is not an *additional* path. The section said "Both agent
        images therefore also install" it while a third, entrypoint-less image
        was installing it as its only launch path (#2317 review)."""
        installers = _dockerfiles_installing_the_launcher()
        assert installers, "no agent Dockerfile installs the OpenShell launcher"

        section = _launcher_section()
        assert "Both agent images" not in section, (
            f"{_CONTRACT_DOC} must not claim two images install the launcher — "
            f"{sorted(installers)} do"
        )
        for name, has_entrypoint in installers.items():
            if has_entrypoint:
                continue
            assert f"docker/{name}" in section, (
                f"{_CONTRACT_DOC} must name docker/{name} in the launcher "
                "section: it ships no entrypoint, so the launcher is its only "
                "launch path"
            )

    def test_quotes_the_real_extraction_constants(self) -> None:
        from kew.sandbox import EXTRACT_ENTRYPOINT, EXTRACT_MOUNT_PATH

        text = _read(_CONTRACT_DOC)
        for const in (EXTRACT_ENTRYPOINT, EXTRACT_MOUNT_PATH):
            assert const in text, (
                f"{_CONTRACT_DOC} must quote kew.sandbox's {const!r} verbatim"
            )

    def test_every_agent_image_installs_the_producer_where_the_host_looks(
        self,
    ) -> None:
        """The contract says the image MUST install the producer at a path, and
        the host launches exactly that path. A Dockerfile that skips the COPY
        fails only at publication time, in a container, on whichever runner was
        picked — so it is asserted here instead."""
        from kew.sandbox import EXTRACT_ENTRYPOINT

        for dockerfile in (
            "docker/agent.Dockerfile",
            "docker/agent-opencode.Dockerfile",
        ):
            text = _read(dockerfile)
            assert f"docker/extract-artifact.sh {EXTRACT_ENTRYPOINT}" in text, (
                f"{dockerfile} must COPY the producer to {EXTRACT_ENTRYPOINT} — "
                "the path build_extract_argv launches"
            )
            assert "--chmod=0755 docker/extract-artifact.sh" in text, (
                f"{dockerfile} must install the producer executable"
            )

    def test_quotes_the_real_verdict_channel(self) -> None:
        """The reviewer half of the contract names the delimiters the host
        parses and the runners that have a typed result record (#2021)."""
        from kew.review_stream import (
            STREAM_VERDICT_RUNNERS,
            VERDICT_BEGIN,
            VERDICT_END,
        )

        text = _read(_CONTRACT_DOC)
        for const in (VERDICT_BEGIN, VERDICT_END, *STREAM_VERDICT_RUNNERS):
            assert const in text, (
                f"{_CONTRACT_DOC} must quote the reviewer verdict channel {const!r}"
            )
        assert ".kew-review-verdict.json" not in text

    def test_quotes_the_real_container_home(self) -> None:
        from kew.sandbox import AGENT_HOME

        assert AGENT_HOME in _read(_CONTRACT_DOC), (
            f"{_CONTRACT_DOC} must quote kew.sandbox.AGENT_HOME ({AGENT_HOME!r})"
        )

    def test_quotes_the_real_credential_env_allowlist(self) -> None:
        """Every runner's declared credentials appear in the contract (#1146).

        The allowlist is per-runner now, so the doc must name each runner's set
        — an image author reading only claude's names would build against
        credentials their container never receives.
        """
        from kew.runners.registry import KNOWN_RUNNER_NAMES
        from kew.sandbox import runner_credential_env_vars

        text = _read(_CONTRACT_DOC)
        for runner in sorted(KNOWN_RUNNER_NAMES):
            for name in runner_credential_env_vars(runner):
                assert name in text, (
                    f"{_CONTRACT_DOC} must name the credential env var {name!r} "
                    f"forwarded for runner {runner!r}"
                )

    def test_covers_all_five_contract_elements(self) -> None:
        text = _read(_CONTRACT_DOC)
        # One distinctive token per element: mounted checkout, prompt→stdin,
        # "$@" extra args, JSON-per-line stdout, reviewer verdict channel.
        for token in ("/work", "stdin", '"$@"', "stream-json", "verdict channel"):
            assert token in text, (
                f"{_CONTRACT_DOC} must cover the contract element keyed by {token!r}"
            )

    def test_names_the_reference_implementation(self) -> None:
        text = _read(_CONTRACT_DOC)
        for path in ("docker/agent.Dockerfile", "docker/agent-entrypoint.sh"):
            assert path in text, (
                f"{_CONTRACT_DOC} must document {path} as the reference implementation"
            )

    def test_states_an_implementer_checklist_normatively(self) -> None:
        text = _read(_CONTRACT_DOC)
        assert "MUST" in text, (
            f"{_CONTRACT_DOC} must state the contract normatively (MUST/MUST NOT)"
        )
        assert "- [ ]" in text or "checklist" in text.lower(), (
            f"{_CONTRACT_DOC} must give image implementers an explicit checklist"
        )


class TestSandboxContractPinsCurrentLimitations:
    """The claude-only caveats stay on the page while they are still true.

    The contract itself is runner-agnostic; the host code driving it is not yet
    (containment-epic nodes N2-N4). Without these gates the "What is built
    today" section could be deleted and every other assertion in this file would
    still pass, restoring the misleading read that any contract-compliant image
    is routable today.

    Each gate is *keyed off the live code*, so it retires by itself: the PR that
    lands N2/N3/N4 makes the guard vacuous rather than red, and the doc row is
    then deleted without a stale test firing on the very change that fixes it.
    """

    def test_single_image_limitation_is_documented(self) -> None:
        from kew.config import SandboxConfig

        fields = SandboxConfig.model_fields
        if fields["image"].annotation is not str or "images" in fields:
            return  # N2 landed: per-runner image selection exists.

        for rel in (_CONTRACT_DOC, "docs/runners/index.md"):
            text = _read(rel)
            assert "single string" in text and "N2" in text, (
                f"{rel} must say [sandbox] image is a single string (no per-runner "
                "map), and name N2 as what closes it — SandboxConfig.image is "
                "still a bare str"
            )

    def test_fixed_claude_argument_block_is_documented(self) -> None:
        import kew.dispatch_sandbox

        literals = source_ast.texts(source_ast.module_tree(kew.dispatch_sandbox))
        if not any("--dangerously-skip-permissions" in text for text in literals):
            return  # N3 landed: extra args are no longer a hardcoded claude block.

        for rel in (_CONTRACT_DOC, "docs/runners/index.md"):
            text = _read(rel)
            assert "--dangerously-skip-permissions" in text and "N3" in text, (
                f"{rel} must quote the fixed claude argument block the host still "
                "appends, and name N3 as what closes it"
            )

    def test_direct_claude_stream_json_parsing_is_documented(self) -> None:
        import kew.dispatch_sandbox

        tree = source_ast.module_tree(kew.dispatch_sandbox)
        if "parse_line" in source_ast.identifiers(tree):
            return  # N4 landed: the sandbox stream goes through the runner parser.

        for rel in (_CONTRACT_DOC, "docs/runners/index.md"):
            text = _read(rel)
            assert "parse_line" in text and "N4" in text, (
                f"{rel} must say the sandbox stream is parsed as claude stream-json "
                "rather than via runner.parse_line(), and name N4 as what closes it"
            )

    def test_non_claude_routing_is_prohibited_in_both_pages(self) -> None:
        from kew.config import SandboxConfig

        if SandboxConfig.model_fields["image"].annotation is not str:
            return  # N2 landed: the prohibition no longer applies.

        for rel in (_CONTRACT_DOC, "docs/runners/index.md"):
            lowered = _read(rel).lower()
            assert "do not route a non-" in lowered, (
                f"{rel} must tell operators not to route a non-claude runner "
                "through the sandbox while image selection is still single-image"
            )


class TestSandboxContractIsSingleSourced:
    """The module docstrings point at the page; they do not re-specify it."""

    def test_sandbox_module_docstring_points_at_the_contract_page(self) -> None:
        import kew.sandbox

        doc = kew.sandbox.__doc__ or ""
        assert "sandbox-entrypoint-contract" in doc, (
            "kew.sandbox's docstring must point at docs/sandbox-entrypoint-contract.md "
            "as the canonical contract text"
        )

    def test_dispatch_sandbox_module_docstring_points_at_the_contract_page(
        self,
    ) -> None:
        # Keep this import standalone: dispatch_sandbox must not need
        # dispatch_core to be primed before its module docstring is available.
        import kew.dispatch_sandbox

        doc = kew.dispatch_sandbox.__doc__ or ""
        assert "sandbox-entrypoint-contract" in doc, (
            "kew.dispatch_sandbox's docstring must point at "
            "docs/sandbox-entrypoint-contract.md as the canonical contract text"
        )

    def test_entrypoint_script_points_at_the_contract_page(self) -> None:
        assert "sandbox-entrypoint-contract" in _read("docker/agent-entrypoint.sh"), (
            "docker/agent-entrypoint.sh must point at the canonical contract page"
        )

    def test_no_intra_module_pointers_to_a_sibling_docstring(self) -> None:
        """Cross-references name the page, never another docstring in the file.

        A "see this module's <topic> docstring" pointer goes stale the moment
        that section is renamed, and nothing catches it — which is exactly what
        happened twice: dispatch_sandbox.py, then sandbox.py's build_run_argv
        still pointing at a "prompt-handoff" section this page had absorbed.
        Pointing at the canonical page instead cannot rot silently, because the
        tests above assert the page exists and is reachable.
        """
        import kew.dispatch_sandbox
        import kew.sandbox

        stale = re.compile(r"this module's [^.]*docstring", re.IGNORECASE)
        for module in (kew.sandbox, kew.dispatch_sandbox):
            for text in source_ast.prose(module):
                hit = stale.search(text)
                assert hit is None, (
                    f"{module.__name__} points at a sibling docstring "
                    f"({hit.group(0)!r}) — cite docs/sandbox-entrypoint-contract.md "
                    "instead, so the reference cannot go stale on a section rename"
                )


# ---------------------------------------------------------------------------
# Node D6: `kew delegate` usage page
# ---------------------------------------------------------------------------


class TestDocumentDelegate:
    """`docs/cli/delegate.md` must not drift from the shipped D1/D5 surface.

    The page is the only place a human learns what assigning an issue to the
    kew seat actually costs them, so a stale flag or a config key that no longer
    exists is a safety problem, not a typo.
    """

    PAGE = "docs/cli/delegate.md"

    def test_documents_every_shipped_delegation_config_key(self) -> None:
        from kew.config import DelegationConfig

        text = _read(self.PAGE)
        for field in DelegationConfig.model_fields:
            assert f"`{field}`" in text, (
                f"{self.PAGE} missing [delegation] key: {field!r}"
            )

    def _documented_default(self, field: str) -> str:
        """The Default cell of the `[delegation]` reference-table row for `field`."""
        for line in _read(self.PAGE).splitlines():
            cells = [cell.strip() for cell in line.split("|")]
            if len(cells) >= 4 and cells[1] == f"`{field}`":
                return cells[2]
        raise AssertionError(
            f"{self.PAGE} has no [delegation] reference-table row for {field!r}"
        )

    def test_documents_the_shipped_defaults(self) -> None:
        """Every field's default, checked in ITS OWN table cell.

        A page-wide `str(value) in text` search passes on a coincidence — `2`,
        `20` and `300` all appear elsewhere on this page — so a field whose
        documented default had drifted would still look documented.
        """
        from kew.config import DelegationConfig

        defaults = DelegationConfig()
        for field in DelegationConfig.model_fields:
            value = getattr(defaults, field)
            expected = f'"{value}"' if isinstance(value, str) else str(value)
            documented = self._documented_default(field)
            assert documented == f"`{expected}`", (
                f"{self.PAGE} documents the default for {field!r} as "
                f"{documented} — the shipped default is `{expected}`"
            )

    def test_documents_only_the_flags_the_command_ships(self) -> None:
        """The documented flag set must EQUAL the shipped one, both directions.

        A one-way check (documented ⊆ shipped) passes silently when a newly
        shipped flag never reaches the page — the drift that matters most on a
        surface where a flag is the difference between a preview and a spend.
        Documented flags are scoped to lines naming the command, so a passing
        reference to another command's flag (e.g. `kew epic run --all`) is not
        miscounted.
        """
        from typer.main import get_command

        from kew.cli import app

        watch = get_command(app).commands["delegate"].commands["watch"]
        shipped = {
            opt for param in watch.params for opt in param.opts if opt.startswith("--")
        }
        documented = {
            flag
            for line in _read(self.PAGE).splitlines()
            if "delegate watch" in line
            for flag in re.findall(r"--[a-z][a-z0-9-]+", line)
        }
        assert documented == shipped, (
            f"{self.PAGE} documents {sorted(documented)} for kew delegate watch; "
            f"the command ships {sorted(shipped)}"
        )
        assert "--dry-run" in documented, (
            f"{self.PAGE} must document the --dry-run preview flag"
        )

    def test_covers_the_member_seat_setup(self) -> None:
        text = _read(self.PAGE)
        for token in ("+kew@", "incognito", "LINEAR_API_KEY"):
            assert token in text, (
                f"{self.PAGE} must cover member-seat setup (missing {token!r})"
            )

    def test_states_that_assignment_alone_authorizes_spend(self) -> None:
        """assign = go: no confirm step, budget caps are the only guard."""
        text = _read(self.PAGE)
        assert "no confirm step" in text.lower(), (
            f"{self.PAGE} must state plainly that there is no confirm step"
        )
        for token in ("per_issue_usd", "daily_usd", "weekly_usd", "max_workers"):
            assert token in text, (
                f"{self.PAGE} must name the cap that guards an assign=go "
                f"dispatch (missing {token!r})"
            )

    def test_points_at_the_dev_tunnel_recipe(self) -> None:
        assert "linear-webhooks.md#dev-tunnel-recipe" in _read(self.PAGE), (
            f"{self.PAGE} must point at the canonical dev-tunnel recipe"
        )
        assert "## Dev-tunnel recipe" in _read("docs/linear-webhooks.md"), (
            "docs/linear-webhooks.md must still carry the dev-tunnel recipe "
            "anchor that docs/cli/delegate.md links to"
        )

    def test_page_is_registered_in_the_mkdocs_nav(self) -> None:
        """Scoped to the `nav:` block, and to an exact `Title: path` entry.

        A whole-file substring match passes on a comment, a `not_in_nav` glob,
        or any unrelated value — so the page could fall out of the nav while
        this test stayed green.
        """
        entry = re.compile(r"^\s+-\s+[^:\n]+:\s*cli/delegate\.md\s*$", re.MULTILINE)
        assert entry.search(_mkdocs_nav_block()) is not None, (
            "docs/cli/delegate.md must be registered in the mkdocs.yml nav"
        )


class TestMigrationDocumentationDrift:
    """Finite CLI and configuration references must match shipped metadata."""

    def test_status_query_failure_codes_match_shipped_constant(self) -> None:
        from kew.cli import GH_QUERY_FAILURE_EXIT_CODE

        text = _read("docs/cli/status.md")
        codes = re.findall(r"(?:exit with code|before exiting) (\d+)\b", text)
        assert codes == [str(GH_QUERY_FAILURE_EXIT_CODE)] * 2
        assert "(exit-codes.md)" in text

    @staticmethod
    def _exit_scheme() -> str:
        path = REPO_ROOT / "docs/cli/exit-codes.md"
        assert path.is_file(), "the published exit-code scheme page is missing"
        return path.read_text()

    @classmethod
    def _assert_exit_scheme(cls, text: str) -> None:
        from typer.main import get_command

        from kew.cli import CONFIG_ERROR_EXIT_CODE, GH_QUERY_FAILURE_EXIT_CODE, app
        from kew.cli.model_eol import MISSING_ROW_EXIT_CODE
        from kew.command_outcome import COULD_NOT_RUN_EXIT_CODE

        def _table_rows() -> list[tuple[str, ...]]:
            section = text.split("## Command families\n", 1)[1].split("\n## ", 1)[0]
            lines = [line for line in section.splitlines() if line.startswith("|")]
            assert lines[:2] == [
                "| Command family | `0` | `1` | `2` |",
                "| --- | --- | --- | --- |",
            ], "the scheme must publish exactly codes 0, 1, and 2"
            rows = [
                tuple(cell.strip() for cell in line.strip("|").split("|"))
                for line in lines[2:]
            ]
            assert all(len(row) == 4 for row in rows), "malformed exit-code table row"
            names = [row[0] for row in rows]
            families = {
                f"`{path[0]}`"
                for path in cls._registered_paths(get_command(app))
                if path
            }
            assert Counter(names) == Counter(families), (
                "exit-code command family inventory drift"
            )
            return rows

        rows = _table_rows()

        documented = {row[0].strip("`"): row[1:] for row in rows}
        bindings = (
            ("run", "GitHub query failure", GH_QUERY_FAILURE_EXIT_CODE),
            ("run", "empty queue", 0),
            ("run", "potentially stranded fleet claim", GH_QUERY_FAILURE_EXIT_CODE),
            ("status", "Unknown run", GH_QUERY_FAILURE_EXIT_CODE),
            ("restore", "Schema too new", CONFIG_ERROR_EXIT_CODE),
            ("restore", "database locked", CONFIG_ERROR_EXIT_CODE),
            ("ci-fix", "PR lookup query failure", GH_QUERY_FAILURE_EXIT_CODE),
            ("ci-fix", "Preparation failure", CONFIG_ERROR_EXIT_CODE),
            ("review", "Pre-launch refusal (not_run)", CONFIG_ERROR_EXIT_CODE),
            ("resolve", "Pre-launch refusal (not_run)", CONFIG_ERROR_EXIT_CODE),
            ("doctor", "live boundary confirmed", 0),
            ("doctor", "live probe refuted boundary", 1),
            ("doctor", "Live confirmation refused or declined", CONFIG_ERROR_EXIT_CODE),
            ("dispatch", "GitHub query failure", GH_QUERY_FAILURE_EXIT_CODE),
            ("status", "GitHub query failure", GH_QUERY_FAILURE_EXIT_CODE),
            ("config", "Configuration failure", CONFIG_ERROR_EXIT_CODE),
            ("models", "Source fetch failure", COULD_NOT_RUN_EXIT_CODE),
            ("models", "fetched value refused", COULD_NOT_RUN_EXIT_CODE),
            ("models", "Refresh written", 0),
            ("model-eol", "Missing lifecycle rows", MISSING_ROW_EXIT_CODE),
            ("model-eol", "Configuration failure", CONFIG_ERROR_EXIT_CODE),
        )
        for family, outcome, shipped in bindings:
            codes = [
                code
                for code, cell in enumerate(documented[family])
                if outcome in cell.split("; ")
            ]
            assert codes == [shipped], (
                f"{family}: {outcome} documents {codes}; shipped code is {shipped}"
            )

    def test_exit_scheme_matches_shipped_codes_and_command_families(self) -> None:
        self._assert_exit_scheme(self._exit_scheme())

    def test_exit_scheme_is_the_linked_published_reference(self) -> None:
        text = self._exit_scheme()
        from kew.cli import CONFIG_ERROR_EXIT_CODE
        from kew.cli.model_eol import MISSING_ROW_EXIT_CODE
        from kew.command_outcome import COULD_NOT_RUN_EXIT_CODE

        assert "two-axis" in text
        assert "did it run" in text
        assert "what did it find" in text
        assert "audit verify` already follows this scheme" in text
        assert "Wrappers must read this scheme page" in text
        assert re.search(
            r"^\s+-\s+[^:\n]+:\s*cli/exit-codes\.md\s*$",
            _mkdocs_nav_block(),
            re.MULTILINE,
        ), "the exit-code scheme must appear in the MkDocs nav"
        for page in (
            "index",
            "epic",
            "model-eol",
            "models",
            "audit",
            "backup",
            "canary",
            "health",
            "delegate",
            "doctor",
            "ingest",
            "restore",
            "skills",
            "status",
            "switchyard",
        ):
            assert "(exit-codes.md)" in _read(f"docs/cli/{page}.md"), page

        def _assert_sibling_exit_claims() -> None:
            assert f"| `{COULD_NOT_RUN_EXIT_CODE}` | A source fetch failed" in _read(
                "docs/cli/models.md"
            )
            assert f"| `{MISSING_ROW_EXIT_CODE}` | At least one" in _read(
                "docs/cli/model-eol.md"
            )
            assert f"| `{CONFIG_ERROR_EXIT_CODE}` | The registry document" in _read(
                "docs/cli/model-eol.md"
            )
            assert f"Commands exit with status `{CONFIG_ERROR_EXIT_CODE}`" in _read(
                "docs/configuration.md"
            )

        _assert_sibling_exit_claims()

    @pytest.mark.parametrize(
        "mutation", ["old-drift", "missing", "duplicate", "unknown"]
    )
    def test_exit_scheme_drift_controls(self, mutation: str) -> None:
        original = self._exit_scheme()
        self._assert_exit_scheme(original)
        row = next(
            line for line in original.splitlines() if line.startswith("| `model-eol` |")
        )
        replacements = {
            "old-drift": row.replace(
                "| Missing lifecycle rows |", "| — | Missing lifecycle rows;"
            ),
            "missing": "",
            "duplicate": row + "\n" + row,
            "unknown": row.replace("`model-eol`", "`unshipped`"),
        }
        with pytest.raises(AssertionError):
            self._assert_exit_scheme(original.replace(row, replacements[mutation]))
        self._assert_exit_scheme(original)

    # One entry per public registration. The tuple key is the Click command path.
    # Hidden metadata commands do not belong in the public documentation contract.
    COMMAND_CASES = (
        ((), "docs/cli/index.md", "Usage: kew [OPTIONS] COMMAND [ARGS]..."),
        (("run",), "docs/cli/run.md", "Usage: kew run [OPTIONS]"),
        (
            ("dispatch",),
            "docs/cli/dispatch.md",
            "Usage: kew dispatch [OPTIONS] ISSUE",
        ),
        (
            ("finalize",),
            "docs/cli/finalize.md",
            "Usage: kew finalize [OPTIONS] RUN_ID",
        ),
        (("init",), "docs/cli/init.md", "Usage: kew init [OPTIONS]"),
        (("schema",), "docs/cli/schema.md", "Usage: kew schema [OPTIONS]"),
        (("setup",), "docs/cli/setup.md", "Usage: kew setup [OPTIONS]"),
        (("status",), "docs/cli/status.md", "Usage: kew status [OPTIONS]"),
        (
            ("review",),
            "docs/cli/review.md",
            "Usage: kew review [OPTIONS] PR_NUMBER",
        ),
        (
            ("resolve",),
            "docs/cli/resolve.md",
            "Usage: kew resolve [OPTIONS] PR_NUMBER",
        ),
        (("ci-fix",), "docs/cli/ci-fix.md", "Usage: kew ci-fix RUN_ID"),
        (("tui",), "docs/tui/index.md", "Usage: kew tui"),
        (
            ("cleanup",),
            "docs/cli/cleanup.md",
            "Usage: kew cleanup [OPTIONS]",
        ),
        (("gc",), "docs/cli/gc.md", "Usage: kew gc [OPTIONS]"),
        (
            ("rename-repo",),
            "docs/cli/rename-repo.md",
            "Usage: kew rename-repo [OPTIONS]",
        ),
        (("stop",), "docs/cli/stop.md", "Usage: kew stop [OPTIONS]"),
        (("kill",), "docs/cli/kill.md", "Usage: kew kill [OPTIONS] RUN_ID"),
        (("settle",), "docs/cli/settle.md", "Usage: kew settle [OPTIONS] RUN_ID"),
        (("resume",), "docs/cli/resume.md", "Usage: kew resume"),
        (("attach",), "docs/cli/attach.md", "Usage: kew attach ISSUE"),
        (("export",), "docs/cli/export.md", "Usage: kew export [OPTIONS]"),
        (("approve",), "docs/cli/approve.md", "Usage: kew approve [OPTIONS] ISSUE"),
        (("deny",), "docs/cli/approve.md", "Usage: kew deny [OPTIONS] ISSUE"),
        (("health",), "docs/cli/health.md", "Usage: kew health [OPTIONS]"),
        (("canary",), "docs/cli/canary.md", "Usage: kew canary [OPTIONS]"),
        (("verify",), "docs/cli/verify.md", "Usage: kew verify [OPTIONS] [REF]"),
        (("backup",), "docs/cli/backup.md", "Usage: kew backup [OPTIONS]"),
        (
            ("restore",),
            "docs/cli/restore.md",
            "Usage: kew restore [OPTIONS] ARCHIVE",
        ),
        (("audit",), "docs/cli/audit.md", "Usage: kew audit COMMAND [ARGS]..."),
        (
            ("audit", "export"),
            "docs/cli/audit.md",
            "Usage: kew audit export [OPTIONS]",
        ),
        (
            ("audit", "verify"),
            "docs/cli/audit.md",
            "Usage: kew audit verify [OPTIONS]",
        ),
        (
            ("audit", "anchor"),
            "docs/cli/audit.md",
            "Usage: kew audit anchor [OPTIONS]",
        ),
        (
            ("audit", "ledger-rebind"),
            "docs/cli/audit.md",
            "Usage: kew audit ledger-rebind [OPTIONS]",
        ),
        (
            ("audit", "policy"),
            "docs/cli/audit.md",
            "Usage: kew audit policy [OPTIONS] [HASH]",
        ),
        (
            ("audit", "tail"),
            "docs/cli/audit.md",
            "Usage: kew audit tail [OPTIONS]",
        ),
        (("loop",), "docs/cli/loop.md", "Usage: kew loop COMMAND [ARGS]..."),
        (
            ("loop", "resume"),
            "docs/cli/loop.md",
            "Usage: kew loop resume [OPTIONS] LOOP_ID",
        ),
        (
            ("loop", "pause"),
            "docs/cli/loop.md",
            "Usage: kew loop pause LOOP_ID",
        ),
        (
            ("config",),
            "docs/cli/config.md",
            "Usage: kew config COMMAND [ARGS]...",
        ),
        (
            ("config", "effective"),
            "docs/cli/config.md",
            "Usage: kew config effective [OPTIONS]",
        ),
        (
            ("ingest",),
            "docs/cli/ingest.md",
            "Usage: kew ingest COMMAND [ARGS]...",
        ),
        (
            ("ingest", "file"),
            "docs/cli/ingest.md",
            "Usage: kew ingest file PATH",
        ),
        (
            ("ingest", "serve"),
            "docs/cli/ingest.md",
            "Usage: kew ingest serve",
        ),
        (
            ("plan",),
            "docs/cli/plan.md",
            "Usage: kew plan COMMAND [ARGS]...",
        ),
        (
            ("plan", "show"),
            "docs/cli/plan.md",
            "Usage: kew plan show [OPTIONS] ITEM",
        ),
        (
            ("triage",),
            "docs/cli/triage.md",
            "Usage: kew triage COMMAND [ARGS]...",
        ),
        (
            ("triage", "queue"),
            "docs/cli/triage.md",
            "Usage: kew triage queue [OPTIONS]",
        ),
        (
            ("triage", "decide"),
            "docs/cli/triage.md",
            "Usage: kew triage decide [OPTIONS] ITEM",
        ),
        (
            ("models",),
            "docs/cli/models.md",
            "Usage: kew models COMMAND [ARGS]...",
        ),
        (
            ("models", "refresh"),
            "docs/cli/models.md",
            "Usage: kew models refresh [OPTIONS]",
        ),
        (
            ("model-eol",),
            "docs/cli/model-eol.md",
            "Usage: kew model-eol COMMAND [ARGS]...",
        ),
        (
            ("model-eol", "check"),
            "docs/cli/model-eol.md",
            "Usage: kew model-eol check [OPTIONS]",
        ),
        (
            ("no-change",),
            "docs/cli/no-change.md",
            "Usage: kew no-change [OPTIONS] COMMAND [ARGS]...",
        ),
        (
            ("no-change", "confirm"),
            "docs/cli/no-change.md",
            "Usage: kew no-change confirm [OPTIONS] ITEM",
        ),
        (
            ("no-change", "reject"),
            "docs/cli/no-change.md",
            "Usage: kew no-change reject [OPTIONS] ITEM",
        ),
        (
            ("no-change", "rejudge"),
            "docs/cli/no-change.md",
            "Usage: kew no-change rejudge [OPTIONS] ITEM",
        ),
        (
            ("no-change", "retry-sync"),
            "docs/cli/no-change.md",
            "Usage: kew no-change retry-sync [OPTIONS] ITEM",
        ),
        (
            ("prompts",),
            "docs/cli/prompts.md",
            "Usage: kew prompts [OPTIONS] COMMAND [ARGS]...",
        ),
        (
            ("prompts", "list"),
            "docs/cli/prompts.md",
            "Usage: kew prompts list [OPTIONS]",
        ),
        (
            ("prompts", "show"),
            "docs/cli/prompts.md",
            "Usage: kew prompts show [OPTIONS] ROLE",
        ),
        (
            ("prompts", "propose"),
            "docs/cli/prompts.md",
            "Usage: kew prompts propose [OPTIONS] ROLE",
        ),
        (
            ("prompts", "approve"),
            "docs/cli/prompts.md",
            "Usage: kew prompts approve [OPTIONS] ROLE VERSION",
        ),
        (
            ("prompts", "decline"),
            "docs/cli/prompts.md",
            "Usage: kew prompts decline [OPTIONS] ROLE VERSION",
        ),
        (
            ("prompts", "retire"),
            "docs/cli/prompts.md",
            "Usage: kew prompts retire [OPTIONS] ROLE",
        ),
        (
            ("merge",),
            "docs/cli/merge.md",
            "Usage: kew merge queue [OPTIONS] COMMAND [ARGS]...",
        ),
        (
            ("merge", "check"),
            "docs/cli/merge.md",
            "Usage: kew merge check [OPTIONS] PR HEAD",
        ),
        (
            ("merge", "queue"),
            "docs/cli/merge.md",
            "Usage: kew merge queue [OPTIONS] COMMAND [ARGS]...",
        ),
        (
            ("merge", "queue", "add"),
            "docs/cli/merge.md",
            "Usage: kew merge queue add [OPTIONS] PR",
        ),
        (
            ("merge", "queue", "list"),
            "docs/cli/merge.md",
            "Usage: kew merge queue list [OPTIONS]",
        ),
        (
            ("merge", "queue", "remove"),
            "docs/cli/merge.md",
            "Usage: kew merge queue remove [OPTIONS] PR",
        ),
        (
            ("merge", "queue", "run"),
            "docs/cli/merge.md",
            "Usage: kew merge queue run [OPTIONS]",
        ),
        (
            ("merge", "claim"),
            "docs/cli/merge.md",
            "Usage: kew merge claim [OPTIONS] COMMAND [ARGS]...",
        ),
        (
            ("merge", "claim", "take"),
            "docs/cli/merge.md",
            "Usage: kew merge claim take [OPTIONS] PR",
        ),
        (
            ("merge", "claim", "renew"),
            "docs/cli/merge.md",
            "Usage: kew merge claim renew [OPTIONS] PR",
        ),
        (
            ("merge", "claim", "release"),
            "docs/cli/merge.md",
            "Usage: kew merge claim release [OPTIONS] PR",
        ),
        (
            ("merge", "claim", "list"),
            "docs/cli/merge.md",
            "Usage: kew merge claim list [OPTIONS]",
        ),
        (
            ("quota",),
            "docs/cli/quota.md",
            "Usage: kew quota [OPTIONS] COMMAND [ARGS]...",
        ),
        (
            ("quota", "clear"),
            "docs/cli/quota.md",
            "Usage: kew quota clear [OPTIONS] RUNNER",
        ),
        (("epic",), "docs/cli/epic.md", "Usage: kew epic COMMAND [ARGS]..."),
        (
            ("epic", "graph"),
            "docs/cli/epic.md",
            "Usage: kew epic graph [OPTIONS] NAME",
        ),
        (
            ("epic", "validate"),
            "docs/cli/epic.md",
            "Usage: kew epic validate [OPTIONS] NAME",
        ),
        (
            ("epic", "plan"),
            "docs/cli/epic.md",
            "Usage: kew epic plan [OPTIONS] NAME",
        ),
        (
            ("epic", "status"),
            "docs/cli/epic.md",
            "Usage: kew epic status [OPTIONS] NAME",
        ),
        (
            ("epic", "heartbeat"),
            "docs/cli/epic.md",
            "Usage: kew epic heartbeat [OPTIONS] NAME",
        ),
        (
            ("epic", "watch"),
            "docs/cli/epic.md",
            "Usage: kew epic watch [OPTIONS] NAME",
        ),
        (
            ("epic", "advance"),
            "docs/cli/epic.md",
            "Usage: kew epic advance [OPTIONS] NAME",
        ),
        (
            ("epic", "run"),
            "docs/cli/epic.md",
            "Usage: kew epic run [OPTIONS] [NAME]",
        ),
        (
            ("epic", "continue"),
            "docs/cli/epic.md",
            "Usage: kew epic continue NAME NODE",
        ),
        (
            ("epic", "abandon"),
            "docs/cli/epic.md",
            "Usage: kew epic abandon [OPTIONS] NAME NODE",
        ),
        (
            ("epic", "retry"),
            "docs/cli/epic.md",
            "Usage: kew epic retry [OPTIONS] NAME NODE",
        ),
        (
            ("epic", "unhold"),
            "docs/cli/epic.md",
            "Usage: kew epic unhold [OPTIONS] NAME ITEM",
        ),
        (
            ("epic", "send-back"),
            "docs/cli/epic.md",
            "Usage: kew epic send-back [OPTIONS] NAME NODE",
        ),
        (
            ("epic", "acceptance"),
            "docs/cli/epic.md",
            "Usage: kew epic acceptance [OPTIONS] COMMAND [ARGS]...",
        ),
        (
            ("epic", "acceptance", "amend"),
            "docs/cli/epic.md",
            "Usage: kew epic acceptance amend [OPTIONS] NAME ITEM",
        ),
        (
            ("epic", "archive"),
            "docs/cli/epic.md",
            "Usage: kew epic archive [OPTIONS] NAME",
        ),
        (
            ("epic", "unarchive"),
            "docs/cli/epic.md",
            "Usage: kew epic unarchive [OPTIONS] NAME",
        ),
        (
            ("epic", "merge"),
            "docs/cli/epic.md",
            "Usage: kew epic merge [OPTIONS] NAME ITEM",
        ),
        (
            ("delegate",),
            "docs/cli/delegate.md",
            "Usage: kew delegate COMMAND [ARGS]...",
        ),
        (
            ("delegate", "watch"),
            "docs/cli/delegate.md",
            "Usage: kew delegate watch [OPTIONS]",
        ),
        (
            ("doctor",),
            "docs/cli/doctor.md",
            "Usage: kew doctor [OPTIONS] [COMMAND] [ARGS]...",
        ),
        (
            ("doctor", "route-live-confirm"),
            "docs/cli/doctor.md",
            "Usage: kew doctor route-live-confirm [OPTIONS]",
        ),
        (
            ("drive",),
            "docs/cli/drive.md",
            "Usage: kew drive [OPTIONS] [COMMAND] [ARGS]...",
        ),
        (("drive", "pause"), "docs/cli/drive.md", "Usage: kew drive pause [EPIC]"),
        (("drive", "resume"), "docs/cli/drive.md", "Usage: kew drive resume [EPIC]"),
        (("drive", "stop"), "docs/cli/drive.md", "Usage: kew drive stop [EPIC]"),
        (("drive", "status"), "docs/cli/drive.md", "Usage: kew drive status [OPTIONS]"),
        (("drive", "hold"), "docs/cli/drive.md", "Usage: kew drive hold"),
        (("drive", "release"), "docs/cli/drive.md", "Usage: kew drive release"),
        (
            ("drive", "install"),
            "docs/cli/drive.md",
            "Usage: kew drive install [OPTIONS]",
        ),
        (("drive", "uninstall"), "docs/cli/drive.md", "Usage: kew drive uninstall"),
        (
            ("run-request",),
            "docs/cli/run-request.md",
            "Usage: kew run-request [OPTIONS] COMMAND [ARGS]...",
        ),
        (
            ("run-request", "queue"),
            "docs/cli/run-request.md",
            "Usage: kew run-request queue [OPTIONS] COMMAND...",
        ),
        (
            ("run-request", "list"),
            "docs/cli/run-request.md",
            "Usage: kew run-request list [OPTIONS]",
        ),
        (
            ("run-request", "show"),
            "docs/cli/run-request.md",
            "Usage: kew run-request show [OPTIONS] REQUEST_ID",
        ),
        (
            ("publication",),
            "docs/cli/publication.md",
            "Usage: kew publication [OPTIONS] COMMAND [ARGS]...",
        ),
        (
            ("hosts",),
            "docs/cli/hosts.md",
            "Usage: kew hosts [OPTIONS] COMMAND [ARGS]...",
        ),
        (
            ("hosts", "provision"),
            "docs/cli/hosts.md",
            "Usage: kew hosts provision [OPTIONS] NAME",
        ),
        (
            ("hosts", "release"),
            "docs/cli/hosts.md",
            "Usage: kew hosts release [OPTIONS] NAME",
        ),
        (
            ("publication", "attest"),
            "docs/cli/publication.md",
            "Usage: kew publication attest [OPTIONS] BRANCH SHA",
        ),
        (("report",), "docs/cli/report.md", "Usage: kew report [OPTIONS]"),
        (
            ("report", "export"),
            "docs/cli/report.md",
            "Usage: kew report export [OPTIONS]",
        ),
        (
            ("report", "provenance"),
            "docs/cli/report.md",
            "Usage: kew report provenance [OPTIONS]",
        ),
        (
            ("report", "sync"),
            "docs/cli/report.md",
            "Usage: kew report sync [OPTIONS]",
        ),
        (
            ("skills",),
            "docs/cli/skills.md",
            "Usage: kew skills COMMAND [ARGS]...",
        ),
        (
            ("skills", "list"),
            "docs/cli/skills.md",
            "Usage: kew skills list [OPTIONS]",
        ),
        (
            ("skills", "install"),
            "docs/cli/skills.md",
            "Usage: kew skills install [OPTIONS] [NAMES]...",
        ),
        (
            ("skills", "update"),
            "docs/cli/skills.md",
            "Usage: kew skills update [OPTIONS] [NAMES]...",
        ),
        (
            ("skills", "diff"),
            "docs/cli/skills.md",
            "Usage: kew skills diff [NAMES]...",
        ),
        (
            ("sandbox",),
            "docs/cli/sandbox.md",
            "Usage: kew sandbox COMMAND [ARGS]...",
        ),
        (
            ("sandbox", "build"),
            "docs/cli/sandbox.md",
            "Usage: kew sandbox build [OPTIONS]",
        ),
        (
            ("sandbox", "certify"),
            "docs/cli/sandbox.md",
            "Usage: kew sandbox certify [OPTIONS]",
        ),
        (
            ("sandbox", "network"),
            "docs/cli/sandbox.md",
            "Usage: kew sandbox network [OPTIONS]",
        ),
        (
            ("sandbox", "smoke"),
            "docs/cli/sandbox.md",
            "Usage: kew sandbox smoke [OPTIONS]",
        ),
        (
            ("serve",),
            "docs/cli/serve.md",
            "Usage: kew serve [OPTIONS] [COMMAND] [ARGS]...",
        ),
        (
            ("serve", "url"),
            "docs/cli/serve.md",
            "Usage: kew serve url",
        ),
        (
            ("switchyard",),
            "docs/cli/switchyard.md",
            "Usage: kew switchyard COMMAND [ARGS]...",
        ),
        (
            ("switchyard", "benchmark"),
            "docs/cli/switchyard.md",
            "Usage: kew switchyard benchmark [OPTIONS]",
        ),
    )
    TOML_PATHS_ONCE = frozenset(
        [
            "agent.budget_stop_grace_seconds",
            "agent.claude_code.autocompact_pct",
            "agent.claude_code.skip_permissions",
            "agent.codex.service_tier",
            "agent.hermes.extra_args",
            "agent.max_budget_usd",
            "agent.no_output_minutes",
            "agent.opencode.agent",
            "agent.timeout_minutes",
            "alerting.notify_on_failure",
            "alerting.notify_on_hold",
            "alerting.notify_on_success",
            "approval.approvers",
            "approval.threshold_usd",
            "audit.anchor_max_age_days",
            "audit.staleness_window",
            "branches.prefix",
            "branches.worktree_base",
            "breaker.anomaly_floor_multiple",
            "breaker.compaction_allowance_factor",
            "breaker.cost_anomaly_factor",
            "breaker.enabled",
            "breaker.min_anomaly_threshold_usd",
            "breaker.min_samples",
            "budget.daily_usd",
            "budget.per_issue_usd",
            "budget.weekly_usd",
            "ci.approval_gated_checks",
            "commands.ci_poll_timeout_seconds",
            "commands.co_author",
            "commands.commit_prefix",
            "commands.format",
            "commands.test_policy_mark",
            "commands.test_targeted",
            "commands.verify_loop",
            "commands.verify_loop_mode",
            "epic.auto_ci_fix",
            "epic.driver_expected_interval_seconds",
            "epic.merge_queue",
            "epic.stall_days",
            "drive.max_parallel",
            "drive.load.thresholds",
            "flight_recorder.expose",
            "flight_recorder.extra_origins",
            "flight_recorder.host",
            "flight_recorder.port",
            "flight_recorder.trusted_proxies",
            "serve.palette_ask",
            "serve.palette_ask_model",
            "human_review.max_diff_lines",
            "human_review.max_paths",
            "human_review.max_pending_reviews",
            "hosts.gcp.idle_stop_minutes",
            "hosts.gcp.instance",
            "hosts.gcp.job_timeout_minutes",
            "hosts.gcp.max_runs",
            "hosts.gcp.instances",
            "hosts.gcp.project",
            "hosts.gcp.zone",
            "hosts.provider",
            "inference_transport.allow_adaptive",
            "inference_transport.mode",
            "inference_transport.switchyard.config_path",
            "inference_transport.switchyard.credential_env",
            "inference_transport.switchyard.expected_version",
            "inference_transport.switchyard.fallback",
            "inference_transport.switchyard.managed",
            "inference_transport.switchyard.route",
            "inference_transport.switchyard.sidecar.egress_network",
            "inference_transport.switchyard.sidecar.enabled",
            "inference_transport.switchyard.sidecar.image",
            "inference_transport.switchyard.startup_timeout_seconds",
            "labels.agent_failed",
            "labels.agent_needs_help",
            "labels.agent_pr",
            "labels.agent_working",
            "labels.blocked",
            "labels.budget_exceeded",
            "labels.ci_approved",
            "labels.completed_no_changes",
            "labels.in_progress",
            "labels.incomplete_run",
            "labels.large_pr",
            "labels.overlap_hold",
            "labels.pr_pending_review",
            "labels.priorities",
            "labels.ready",
            "labels.ready_to_merge",
            "logging.dir",
            "logging.retention_days",
            "parallel.max_workers",
            "parallel.shutdown_grace_seconds",
            "project.name",
            "prompt.include_coderabbit",
            "prompt.include_dispatch_report",
            "prompt.include_execution_strategy",
            "prompt.include_repo_map",
            "prompt.include_safety_rules",
            "prompt.repo_map_mode",
            "prompt.repo_map_tokens",
            "publication.publish",
            "publication.shadow",
            "publication.shadow_evidence",
            "retry.backoff_base_seconds",
            "retry.backoff_multiplier",
            "retry.max_attempts",
            "retry.max_resume_attempts",
            "retry.retriable_exit_codes",
            "review.adversarial",
            "review.auto_reply",
            "review.blocking",
            "review.include_resolved",
            "review.max_remediation_usd",
            "review.max_review_usd",
            "review.max_rounds",
            "review.model",
            "review.publish_status",
            "review.runner",
            "review.stop_rule_after_rounds",
            "review.stop_rule_max_severity",
            "routing.cache.governor_threshold_usd",
            "routing.cache.max_reorder_lookahead",
            "routing.cache.order_aware",
            "routing.cache_aware",
            "routing.simple.model",
            "routing.simple.runner",
            "run_reports.enabled",
            "run_reports.sink_path",
            "sandbox.dangerously_skip_containment",
            "sandbox.enabled",
            "sandbox.host",
            "sandbox.image",
            "sandbox.images.claude-code",
            "sandbox.network",
            "sandbox.openshell.gateway",
            "sandbox.openshell.providers.claude-code",
            "sandbox.openshell.version",
            "sandbox.proxy_environment",
            "sandbox.runtime",
            "security.blocked_exact_patterns",
            "security.blocked_prefix_patterns",
            "tracker.linear.in_review_state",
            "triage.backend",
            "triage.dedupe_candidates",
            "triage.enabled",
            "triage.fallback_backend",
            "triage.human_types",
            "triage.intake_label",
            "triage.max_body_chars",
            "triage.max_usd_per_item",
            "triage.min_confidence",
            "triage.model",
            "triage.skip_epic_nodes",
            "tracker.linear.reconcile_interval_seconds",
            "tracker.linear.team",
            "tracker.type",
            "triggers.ci_failure_max_retries",
            "drive.merge.enabled",
            "drive.merge.exclude_epics",
            "drive.merge.gate",
            "drive.merge.local_fallback",
            "drive.merge.signer",
            "drive.merge.ui_paths",
            "merge_queue.train",
            "paths.work_root",
            "verify.deselect",
            "verify.host",
            "verify.timeout_seconds",
        ]
    )
    TOML_PATHS_TWICE = frozenset(
        [
            "agent.codex.reasoning_effort",
            "agent.lemonade.base_url",
            "agent.lemonade.extra_args",
            "agent.lemonade.max_steps",
            "agent.opencode.extra_args",
            "agent.pi.extra_args",
            "approval.label",
            "billing.mode",
            "billing.plan_label",
            "billing.plan_monthly_usd",
            "commands.ci",
            "commands.verify",
            "delegation.assignee",
            "delegation.fast_interval_seconds",
            "delegation.label",
            "delegation.poll_interval_seconds",
            "delegation.refusal_cooldown_seconds",
            "ingest.cap_usd",
            "ingest.combine_with_dispatch",
            "ingest.enabled",
            "ingest.host",
            "ingest.port",
            "overlap.advanced.merge_context_ttl_minutes",
            "overlap.strategy",
            "resolve.auto",
            "resolve.strategy",
            "sandbox.images.codex",
            "sandbox.images.opencode",
        ]
    )
    # Every [routing] snippet carries its required `default` (F17).
    TOML_PATHS_THRICE = frozenset({"routing.default"})
    INTENTIONAL_TOML_VALUES = {
        ("project.name", 0): "my-project",
        ("agent.max_budget_usd", 0): 10.0,
        ("inference_transport.switchyard.route", 0): "switchyard/passthrough",
        ("inference_transport.switchyard.expected_version", 0): "0.1.0",
        ("inference_transport.switchyard.managed", 0): False,
        (
            "inference_transport.switchyard.config_path",
            0,
        ): "/absolute/operator-owned/switchyard.toml",
        ("inference_transport.switchyard.credential_env", 0): ["OPENAI_API_KEY"],
        ("inference_transport.switchyard.startup_timeout_seconds", 0): 20,
        ("inference_transport.switchyard.fallback", 0): "refuse",
        ("inference_transport.switchyard.sidecar.enabled", 0): False,
        (
            "inference_transport.switchyard.sidecar.image",
            0,
        ): "kew-switchyard:operator-built",
        (
            "inference_transport.switchyard.sidecar.egress_network",
            0,
        ): "kew-allowlist",
        ("security.blocked_prefix_patterns", 0): ["kubectl delete"],
        ("security.blocked_exact_patterns", 0): ["DROP DATABASE"],
        ("commands.ci", 1): "make test",
        ("commands.verify", 1): "make test-fast",
        ("approval.threshold_usd", 0): 5.0,
        ("approval.label", 1): "needs-approval",
        ("approval.approvers", 0): ["alice", "bob"],
        ("drive.load.thresholds", 0): [
            {"above": 14, "max_parallel": 2},
            {"above": 25, "max_parallel": 1},
        ],
        ("routing.simple.runner", 0): "codex",
        ("routing.simple.model", 0): "gpt-5.6-sol",
        ("agent.codex.reasoning_effort", 1): "high",
        ("agent.codex.service_tier", 0): "fast",
        ("sandbox.images.opencode", 0): "kew-agent-opencode:docker-d148816b388f",
        ("sandbox.images.codex", 0): "kew-agent-codex:docker-d148816b388f",
        ("sandbox.images.claude-code", 0): "kew-agent:docker-d148816b388f",
        ("sandbox.images.opencode", 1): "kew-agent-opencode:docker-d148816b388f",
        ("sandbox.images.codex", 1): "kew-agent-codex:docker-d148816b388f",
        ("sandbox.openshell.providers.claude-code", 0): "kew-claude-code",
    }
    CONDITIONAL_TOML_VALUES = {
        ("routing.default", 0): "claude-code",
        ("routing.default", 1): "claude-code",
        ("routing.default", 2): "claude-code",
    }
    COMMENTED_TOML_PATHS_ONCE = frozenset(
        [
            "agent.codex.service_tier",
            "agent.model",
            "agent.opencode.agent",
            "agent.runner",
            "alerting.webhook_url",
            "approval.approvers",
            "approval.threshold_usd",
            "branches.base",
            "drive.max_load_per_cpu",
            "drive.max_swap_used_pct",
            "epic.approvers",
            "epic.lock_path",
            "epic.max_parallel",
            "human_review.approvers",
            "inference_transport.switchyard.binary",
            "inference_transport.switchyard.binary_sha256",
            "inference_transport.switchyard.compatibility_record_path",
            "inference_transport.switchyard.endpoint",
            "pricing.overrides",
            "project.repo",
            "prompt.append",
            "prompt.template",
            "routing.complex",
            "routing.medium",
            "routing.simple",
            "sandbox.allow_uncontained_flows",
            "sandbox.hosts.dispatch",
            "sandbox.max_cpus",
            "sandbox.max_memory",
            "sandbox.placement",
        ]
    )
    COMMENTED_TOML_PATHS_TWICE = frozenset(
        {"agent.lemonade.api_key", "fleet.max_concurrent"}
    )
    INTENTIONAL_COMMENTED_PATHS = frozenset(
        {
            "epic.approvers",
            "human_review.approvers",
            "pricing.overrides",
            "routing.complex",
            "routing.medium",
            "routing.simple",
            "sandbox.hosts.dispatch",
            "sandbox.placement",
        }
    )
    CHECKED_COMMENTED_DEFAULTS = frozenset({"approval.approvers"})
    UNSET_COMMENTED_DEFAULTS = frozenset(
        {
            "approval.threshold_usd",
            "fleet.max_concurrent",
            "drive.max_load_per_cpu",
            "drive.max_swap_used_pct",
            "epic.lock_path",
            "epic.max_parallel",
            "sandbox.allow_uncontained_flows",
            "sandbox.max_cpus",
            "sandbox.max_memory",
        }
    )
    EXPECTED_TOML_SECTIONS = frozenset(
        [
            "project",
            "agent",
            "agent.claude_code",
            "agent.opencode",
            "agent.codex",
            "agent.pi",
            "agent.hermes",
            "agent.lemonade",
            "branches",
            "ci",
            "commands",
            "prompt",
            "security",
            "retry",
            "parallel",
            "fleet",
            "review",
            "resolve",
            "triage",
            "labels",
            "triggers",
            "delegation",
            "budget",
            "breaker",
            "approval",
            "human_review",
            "audit",
            "publication",
            "epic",
            "drive",
            "drive.load",
            "routing",
            "routing.cache",
            "pricing",
            "inference_transport",
            "inference_transport.switchyard",
            "inference_transport.switchyard.sidecar",
            "billing",
            "alerting",
            "ingest",
            "logging",
            "tracker",
            "tracker.linear",
            "flight_recorder",
            "serve",
            "overlap",
            "overlap.advanced",
            "run_reports",
            "sandbox",
            "sandbox.hosts",
            "sandbox.images",
            "sandbox.openshell",
            "sandbox.openshell.providers",
            "hosts",
            "hosts.gcp",
            "drive.merge",
            "merge_queue",
            "paths",
            "verify",
        ]
    )
    DEFAULT_CELL_CASES = (
        ("## `[routing.cache]`", "cache_aware", "routing.cache_aware"),
        ("## `[routing.cache]`", "order_aware", "routing.cache.order_aware"),
        (
            "## `[routing.cache]`",
            "governor_threshold_usd",
            "routing.cache.governor_threshold_usd",
        ),
        (
            "## `[routing.cache]`",
            "max_reorder_lookahead",
            "routing.cache.max_reorder_lookahead",
        ),
        ("## `[audit]`", "staleness_window", "audit.staleness_window"),
        ("## `[audit]`", "anchor_max_age_days", "audit.anchor_max_age_days"),
    )
    DEFAULT_PROSE_CASES = (
        (
            "## Inference transport",
            "allow_adaptive",
            "inference_transport.allow_adaptive",
        ),
        ("## `[delegation]`", "assignee", "delegation.assignee"),
        ("## `[delegation]`", "label", "delegation.label"),
        (
            "## `[delegation]`",
            "poll_interval_seconds",
            "delegation.poll_interval_seconds",
        ),
        (
            "## `[delegation]`",
            "fast_interval_seconds",
            "delegation.fast_interval_seconds",
        ),
        (
            "## `[delegation]`",
            "refusal_cooldown_seconds",
            "delegation.refusal_cooldown_seconds",
        ),
        ("## `[approval]`", "approvers", "approval.approvers"),
        (
            "## `[approval]`",
            "[alerting] notify_on_hold",
            "alerting.notify_on_hold",
        ),
        ("## `[flight_recorder]`", "host", "flight_recorder.host"),
        ("## `[flight_recorder]`", "port", "flight_recorder.port"),
        ("## `[flight_recorder]`", "expose", "flight_recorder.expose"),
        (
            "## `[flight_recorder]`",
            "extra_origins",
            "flight_recorder.extra_origins",
        ),
        (
            "## `[flight_recorder]`",
            "trusted_proxies",
            "flight_recorder.trusted_proxies",
        ),
        ("## `[serve]`", "palette_ask", "serve.palette_ask"),
        ("## `[serve]`", "palette_ask_model", "serve.palette_ask_model"),
        ("## `[ingest]`", "enabled", "ingest.enabled"),
        ("## `[ingest]`", "host", "ingest.host"),
        ("## `[ingest]`", "port", "ingest.port"),
        ("## `[ingest]`", "cap_usd", "ingest.cap_usd"),
        (
            "## `[ingest]`",
            "combine_with_dispatch",
            "ingest.combine_with_dispatch",
        ),
        ("## `[billing]`", "mode", "billing.mode"),
        ("## `[billing]`", "plan_label", "billing.plan_label"),
        (
            "## `[billing]`",
            "plan_monthly_usd",
            "billing.plan_monthly_usd",
        ),
        ("## `[overlap]`", "strategy", "overlap.strategy"),
        (
            "## `[overlap]`",
            "[overlap.advanced].merge_context_ttl_minutes",
            "overlap.advanced.merge_context_ttl_minutes",
        ),
        ("## `[overlap]`", "overlap_hold", "labels.overlap_hold"),
        ("## `[resolve]`", "strategy", "resolve.strategy"),
        ("## `[resolve]`", "auto", "resolve.auto"),
        ("## `[publication]`", "publish", "publication.publish"),
        ("## `[publication]`", "shadow", "publication.shadow"),
        (
            "## `[publication]`",
            "shadow_evidence",
            "publication.shadow_evidence",
        ),
        ("## `[epic]`", "max_parallel", "epic.max_parallel"),
        ("## `[epic]`", "stall_days", "epic.stall_days"),
        (
            "## `[epic]`",
            "driver_expected_interval_seconds",
            "epic.driver_expected_interval_seconds",
        ),
        ("## `[epic]`", "approvers", "epic.approvers"),
        ("## `[epic]`", "auto_ci_fix", "epic.auto_ci_fix"),
        ("## `[epic]`", "merge_queue", "epic.merge_queue"),
        ("## `[epic]`", "lock_path", "epic.lock_path"),
        ("## `[drive]`", "max_parallel", "drive.max_parallel"),
        ("## `[drive]`", "load.thresholds", "drive.load.thresholds"),
        ("## `[drive]`", "max_load_per_cpu", "drive.max_load_per_cpu"),
        ("## `[drive]`", "max_swap_used_pct", "drive.max_swap_used_pct"),
        ("## `[run_reports]`", "enabled", "run_reports.enabled"),
        ("## `[run_reports]`", "sink_path", "run_reports.sink_path"),
        ("## `[sandbox]`", "enabled", "sandbox.enabled"),
        (
            "## `[sandbox]`",
            "dangerously_skip_containment",
            "sandbox.dangerously_skip_containment",
        ),
        ("## `[sandbox]`", "runtime", "sandbox.runtime"),
        ("## `[sandbox]`", "image", "sandbox.image"),
        ("## `[sandbox]`", "images", "sandbox.images"),
        ("## `[sandbox]`", "network", "sandbox.network"),
        ("## `[sandbox]`", "proxy_environment", "sandbox.proxy_environment"),
    )
    PROSE_DEFAULT_COUNTS = {"inference_transport.allow_adaptive": 2}
    TEXT_FENCE = re.compile(
        r"^```text[ \t]*\n(?P<body>.*?)^```[ \t]*$",
        re.MULTILINE | re.DOTALL,
    )
    TOML_FENCE = re.compile(
        r"^(?P<indent>[ \t]*)```toml[ \t]*\n(?P<body>.*?)^(?P=indent)```[ \t]*$",
        re.MULTILINE | re.DOTALL,
    )
    COMPLETE_CONFIGURATION_MARKER = "# This complete table "
    SIDECAR_SWITCHYARD_REFUSAL = "sidecar switchyard requires switchyard transport mode"
    TOML_SECTION = re.compile(r"^\[([^\]\n]+)\][ \t]*(?:#.*)?$", re.MULTILINE)
    OPTION_LINE = re.compile(r"^  --?[a-zA-Z]")

    @classmethod
    def _flatten_toml(
        cls,
        data: Mapping[str, object],
        prefix: tuple[str, ...] = (),
    ) -> dict[str, object]:
        flattened: dict[str, object] = {}
        for key, value in data.items():
            path = (*prefix, key)
            if isinstance(value, Mapping):
                flattened.update(cls._flatten_toml(value, path))
            else:
                flattened[".".join(path)] = value
        return flattened

    @classmethod
    def _documented_toml_values(cls, text: str) -> dict[str, list[object]]:
        documented: dict[str, list[object]] = {}
        for fence in cls.TOML_FENCE.finditer(text):
            parsed = tomllib.loads(fence.group("body"))
            for path, value in cls._flatten_toml(parsed).items():
                documented.setdefault(path, []).append(value)
        return documented

    @classmethod
    def _documented_toml_sections(cls, text: str) -> set[str]:
        return {
            match.group(1)
            for fence in cls.TOML_FENCE.finditer(text)
            for match in cls.TOML_SECTION.finditer(fence.group("body"))
        }

    @classmethod
    def _complete_configuration(cls, text: str) -> dict[str, object]:
        matches = [
            fence
            for fence in cls.TOML_FENCE.finditer(text)
            if cls.COMPLETE_CONFIGURATION_MARKER in fence.group("body")
        ]
        assert len(matches) == 1, (
            f"expected one complete configuration fence, found {len(matches)}"
        )
        return tomllib.loads(matches[0].group("body"))

    @classmethod
    def _assert_complete_configuration_valid(cls, text: str) -> None:
        from pydantic import ValidationError

        from kew.config import DispatchConfig

        try:
            DispatchConfig.model_validate(cls._complete_configuration(text))
        except ValidationError as exc:
            reasons = [
                str(error.get("ctx", {}).get("error", error["msg"]))
                for error in exc.errors()
            ]
            assert not reasons, (
                "DispatchConfig refused complete configuration: " + "; ".join(reasons)
            )

    @staticmethod
    def _markdown_section(text: str, heading: str) -> str:
        start = text.find(heading)
        assert start >= 0, f"missing configuration section {heading}"
        following = text.find("\n## ", start + len(heading))
        return text[start:] if following < 0 else text[start:following]

    @staticmethod
    def _render_default(value: object) -> str:
        if value is None:
            return "unset"
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, str):
            return f'"{value}"'
        if isinstance(value, (list, dict)):
            return json.dumps(value, sort_keys=True)
        return str(value)

    @classmethod
    def _assert_default_cells(
        cls,
        text: str,
        source_defaults: Mapping[str, object],
    ) -> None:
        for heading, key, source_path in cls.DEFAULT_CELL_CASES:
            section = cls._markdown_section(text, heading)
            pattern = re.compile(
                rf"^\| `{re.escape(key)}` \| `(?P<value>[^`]+)` \|",
                re.MULTILINE,
            )
            matches = list(pattern.finditer(section))
            assert len(matches) == 1, (
                f"expected one default cell for {heading} {key}, found {len(matches)}"
            )
            expected = cls._render_default(source_defaults[source_path])
            documented = matches[0].group("value")
            assert documented == expected, (
                f"{source_path} default cell documents {documented!r}; "
                f"the shipped default is {expected!r}"
            )

    @classmethod
    def _assert_prose_defaults(
        cls,
        text: str,
        source_defaults: Mapping[str, object],
    ) -> None:
        for heading, key, source_path in cls.DEFAULT_PROSE_CASES:
            section = cls._markdown_section(text, heading)
            key_token = (
                f"`{key}`"
                if source_path
                in {"inference_transport.allow_adaptive", "labels.overlap_hold"}
                else f"**`{key}`**"
            )
            lines = [
                line
                for line in section.splitlines()
                if key_token in line and "default" in line.casefold()
            ]
            expected_count = cls.PROSE_DEFAULT_COUNTS.get(source_path, 1)
            assert len(lines) == expected_count, (
                f"expected one prose default for {heading} {key}, found {len(lines)}"
            )
            expected = cls._render_default(source_defaults[source_path])
            if expected == "unset":
                default_claim = re.compile(r"\bdefault\s*:\s*unset\b", re.IGNORECASE)
            else:
                default_claim = re.compile(
                    rf"\bdefaults?(?:\s+to|\s*:)?\s+`{re.escape(expected)}`",
                    re.IGNORECASE,
                )
            for line in lines:
                assert default_claim.search(line), (
                    f"{source_path} prose default does not equal source value "
                    f"{expected!r}: {line}"
                )

    @classmethod
    def _commented_toml_values(cls, text: str) -> dict[str, list[object]]:
        documented: dict[str, list[object]] = {}
        assignment = re.compile(
            r"^\s*#\s*(?P<key>[a-zA-Z][\w.-]*)\s*=\s*(?P<value>.*?)(?:\s+#.*)?$"
        )
        for fence in cls.TOML_FENCE.finditer(text):
            section = ""
            for line in fence.group("body").splitlines():
                header = re.match(r"^\[([^\]]+)\]", line)
                if header is not None:
                    section = header.group(1)
                    continue
                match = assignment.match(line)
                if match is None:
                    continue
                parsed = tomllib.loads(f"value = {match.group('value')}")
                path = f"{section}.{match.group('key')}"
                documented.setdefault(path, []).append(parsed["value"])
        return documented

    @classmethod
    def _assert_commented_toml_coverage(
        cls,
        text: str,
        source_defaults: Mapping[str, object],
    ) -> None:
        documented = cls._commented_toml_values(text)
        expected = Counter({path: 1 for path in cls.COMMENTED_TOML_PATHS_ONCE})
        expected.update({path: 2 for path in cls.COMMENTED_TOML_PATHS_TWICE})
        observed = Counter({path: len(values) for path, values in documented.items()})
        assert observed == expected, (
            f"commented TOML paths {observed} differ from the finite coverage map "
            f"{expected}"
        )
        for path in cls.CHECKED_COMMENTED_DEFAULTS:
            assert documented[path] == [source_defaults[path]]
        for path in cls.UNSET_COMMENTED_DEFAULTS:
            assert source_defaults[path] is None

        conditional = set(expected) - set(cls.INTENTIONAL_COMMENTED_PATHS)
        conditional -= cls.CHECKED_COMMENTED_DEFAULTS
        dispositions = (
            conditional
            | set(cls.INTENTIONAL_COMMENTED_PATHS)
            | cls.CHECKED_COMMENTED_DEFAULTS
        )
        assert dispositions == set(expected)

    @classmethod
    def _source_default_values(
        cls,
        monkeypatch: pytest.MonkeyPatch,
    ) -> dict[str, object]:
        import kew.config as config_module
        from kew.config import CacheConfig, DispatchConfig, RoutingConfig

        monkeypatch.delenv("KEW_LOG_DIR", raising=False)
        monkeypatch.setattr(config_module, "_DEFAULT_LOG_DIR_OVERRIDE", None)
        monkeypatch.setattr(config_module, "_DEFAULT_WORKTREE_BASE_OVERRIDE", None)

        config = DispatchConfig()
        defaults = cls._flatten_toml(config.model_dump(by_alias=True))
        defaults["sandbox.images"] = config.sandbox.images
        defaults["routing.cache_aware"] = RoutingConfig.model_fields[
            "cache_aware"
        ].default
        for field, value in CacheConfig().model_dump().items():
            defaults[f"routing.cache.{field}"] = value
        return defaults

    @classmethod
    def _assert_toml_coverage_and_defaults(
        cls,
        text: str,
        source_defaults: Mapping[str, object],
    ) -> None:
        documented = cls._documented_toml_values(text)
        expected_counts = Counter({path: 1 for path in cls.TOML_PATHS_ONCE})
        expected_counts.update({path: 2 for path in cls.TOML_PATHS_TWICE})
        expected_counts.update({path: 3 for path in cls.TOML_PATHS_THRICE})
        observed_counts = Counter(
            {path: len(values) for path, values in documented.items()}
        )
        assert observed_counts == expected_counts, (
            f"documented TOML paths {observed_counts} differ from the finite "
            f"coverage map {expected_counts}"
        )

        classified = set(cls.INTENTIONAL_TOML_VALUES) | set(cls.CONDITIONAL_TOML_VALUES)

        def _assert_documented_defaults() -> None:
            for path, values in documented.items():
                for occurrence, documented_value in enumerate(values):
                    case = (path, occurrence)
                    if case in cls.INTENTIONAL_TOML_VALUES:
                        expected = cls.INTENTIONAL_TOML_VALUES[case]
                        assert documented_value == expected, (
                            f"intentional example {path} changed from {expected!r} "
                            f"to {documented_value!r}"
                        )
                        continue
                    if case in cls.CONDITIONAL_TOML_VALUES:
                        expected = cls.CONDITIONAL_TOML_VALUES[case]
                        assert documented_value == expected, (
                            f"conditional value {path} changed from {expected!r} "
                            f"to {documented_value!r}"
                        )
                        continue
                    assert path in source_defaults, (
                        f"checked default {path} has no source"
                    )
                    assert documented_value == source_defaults[path], (
                        f"{path} documents {documented_value!r}; the shipped default "
                        f"is "
                        f"{source_defaults[path]!r}"
                    )

        _assert_documented_defaults()

        expected_cases = {
            (path, occurrence)
            for path, count in expected_counts.items()
            for occurrence in range(count)
        }
        checked = expected_cases - classified
        assert checked | classified == expected_cases
        assert checked.isdisjoint(classified)

    @classmethod
    def _usage_block_match(cls, text: str, usage_header: str) -> re.Match[str]:
        usage = re.compile(rf"^{re.escape(usage_header)}[ \t]*$", re.MULTILINE)
        matches = [
            match
            for match in cls.TEXT_FENCE.finditer(text)
            if usage.search(match.group("body")) is not None
        ]
        assert len(matches) == 1, (
            f"expected one text usage block for {usage_header!r}, found {len(matches)}"
        )
        return matches[0]

    @classmethod
    def _documented_options(cls, text: str, usage_header: str) -> set[str]:
        block = cls._usage_block_match(text, usage_header).group("body")
        _, separator, option_section = block.partition("\nOptions:\n")
        if not separator:
            return set()

        documented: set[str] = set()
        for line in option_section.splitlines():
            if cls.OPTION_LINE.match(line) is None:
                continue
            declaration = re.split(r"\s{2,}", line.strip(), maxsplit=1)[0]
            documented.update(re.findall(r"(?<!\w)--?[a-z][a-z0-9-]*", declaration))
        return documented

    @classmethod
    def _serve_no_expose_description(cls, text: str) -> str:
        header = "Usage: kew serve [OPTIONS] [COMMAND] [ARGS]..."
        block = cls._usage_block_match(text, header).group("body")
        matches = re.findall(
            r"^  --no-expose\b.*?(?=^  --|\Z)",
            block,
            re.MULTILINE | re.DOTALL,
        )
        assert len(matches) == 1, (
            f"expected one --no-expose description, found {len(matches)}"
        )
        return " ".join(matches[0].split())

    @classmethod
    def _assert_serve_no_expose_matches_resolver(cls, text: str) -> None:
        import typer

        from kew.cli.serve import _serve_resolve_bind
        from kew.config import DispatchConfig, FlightRecorderConfig

        configured = DispatchConfig(flight_recorder=FlightRecorderConfig(expose=True))
        _, _, effective_expose = _serve_resolve_bind(
            configured,
            "192.0.2.1",
            0,
            False,
        )
        assert effective_expose is True

        unconfigured = DispatchConfig(flight_recorder=FlightRecorderConfig())
        with pytest.raises(typer.Exit) as refusal:
            _serve_resolve_bind(unconfigured, "192.0.2.1", 0, False)
        assert refusal.value.exit_code == 2

        description = cls._serve_no_expose_description(text)
        assert "cannot override [flight_recorder].expose = true" in description, (
            "--no-expose must state that configured exposure remains enabled"
        )

    @classmethod
    def _assert_audit_policy_usage_matches_click(cls, text: str) -> None:
        import click
        from typer.main import get_command

        from kew.cli import app

        documented = [
            line.strip()
            for fence in cls.TEXT_FENCE.finditer(text)
            for line in fence.group("body").splitlines()
            if line.startswith("Usage: kew audit policy")
        ]
        assert len(documented) == 1, (
            f"expected one audit policy usage header, found {len(documented)}"
        )

        command = get_command(app).commands["audit"].commands["policy"]
        context = click.Context(command, info_name="kew audit policy")
        shipped = command.get_usage(context).strip()
        assert documented[0] == shipped, (
            f"audit policy documents {documented[0]!r}; Click ships {shipped!r}"
        )

    @staticmethod
    def _shipped_options(command_path: tuple[str, ...]) -> set[str]:
        from typer.main import get_command

        from kew.cli import app

        command = get_command(app)
        for name in command_path:
            assert hasattr(command, "commands"), (
                f"registered command path ends before {name!r}"
            )
            command = command.commands[name]
        shipped = {
            option
            for parameter in command.params
            for option in (*parameter.opts, *parameter.secondary_opts)
            if option.startswith("-") and option != "--help"
        }
        return shipped

    @classmethod
    def _registered_paths(
        cls, command: object, path: tuple[str, ...] = ()
    ) -> set[tuple[str, ...]]:
        paths = {path}
        for name, child in getattr(command, "commands", {}).items():
            if getattr(child, "hidden", False):
                continue
            paths.update(cls._registered_paths(child, (*path, name)))
        return paths

    @classmethod
    def _assert_command_coverage(
        cls,
        cases: tuple[tuple[tuple[str, ...], str, str], ...],
        command: object,
    ) -> None:
        documented = [case[0] for case in cases]
        assert len(documented) == len(set(documented)), "duplicate command path mapping"
        registered = cls._registered_paths(command)
        assert set(documented) == registered, (
            f"documented paths {sorted(documented)} differ from public registrations "
            f"{sorted(registered)}"
        )

    @classmethod
    def _assert_options_match(
        cls,
        text: str,
        command_path: tuple[str, ...],
        usage_header: str,
    ) -> None:
        documented = cls._documented_options(text, usage_header)
        shipped = cls._shipped_options(command_path)
        assert documented == shipped, (
            f"{' '.join(('kew', *command_path))} documents {sorted(documented)}; "
            f"the command ships {sorted(shipped)}"
        )

    @classmethod
    def _replace_usage_body(
        cls,
        text: str,
        usage_header: str,
        transform: Callable[[str], str],
    ) -> str:
        match = cls._usage_block_match(text, usage_header)
        body = match.group("body")
        replacement = transform(body)
        assert replacement != body, "control did not change the target usage block"
        return text[: match.start("body")] + replacement + text[match.end("body") :]

    @classmethod
    def _remove_option(cls, text: str, usage_header: str, option: str) -> str:
        def remove(body: str) -> str:
            lines = body.splitlines(keepends=True)
            matches = [
                index
                for index, line in enumerate(lines)
                if cls.OPTION_LINE.match(line) is not None
                and option
                in re.findall(
                    r"(?<!\w)--?[a-z][a-z0-9-]*",
                    re.split(r"\s{2,}", line.strip(), maxsplit=1)[0],
                )
            ]
            assert len(matches) == 1, (
                f"expected one {option} declaration in {usage_header} help"
            )
            lines.pop(matches[0])
            return "".join(lines)

        return cls._replace_usage_body(text, usage_header, remove)

    @classmethod
    def _add_unshipped_option(cls, text: str, usage_header: str) -> str:
        def add(body: str) -> str:
            marker = "Options:\n"
            if marker not in body:
                return (
                    body.rstrip()
                    + "\n\nOptions:\n  --not-shipped  Control-only option\n"
                )
            assert body.count(marker) == 1
            return body.replace(
                marker,
                marker + "  --not-shipped  Control-only option\n",
                1,
            )

        return cls._replace_usage_body(text, usage_header, add)

    @classmethod
    def _budget_table_span(cls, text: str) -> tuple[int, int]:
        spans: list[tuple[int, int]] = []
        header_pattern = re.compile(r"^\[budget\][ \t]*(?:#.*)?$", re.MULTILINE)
        next_header_pattern = re.compile(
            r"^\s*\[{1,2}[^\]\n]+\]{1,2}\s*(?:#.*)?$",
            re.MULTILINE,
        )
        for fence in cls.TOML_FENCE.finditer(text):
            body = fence.group("body")
            for header in header_pattern.finditer(body):
                following = body[header.end() :]
                next_header = next_header_pattern.search(following)
                end = (
                    header.end() + next_header.start()
                    if next_header is not None
                    else len(body)
                )
                spans.append(
                    (fence.start("body") + header.start(), fence.start("body") + end)
                )
        assert len(spans) == 1, (
            f"expected one fenced TOML [budget] table, found {len(spans)}"
        )
        return spans[0]

    @classmethod
    def _documented_budget(cls, text: str) -> dict[str, object]:
        start, end = cls._budget_table_span(text)
        parsed = tomllib.loads(text[start:end])
        assert set(parsed) == {"budget"}, "budget selection included another table"
        budget = parsed["budget"]
        assert isinstance(budget, dict), "documented [budget] value is not a table"
        return budget

    @classmethod
    def _assert_budget_matches(cls, text: str) -> None:
        from kew.config import BudgetConfig

        documented = cls._documented_budget(text)
        shipped = BudgetConfig().model_dump()
        assert set(documented) == set(shipped), (
            f"documented [budget] keys {sorted(documented)} differ from "
            f"shipped keys {sorted(shipped)}"
        )
        for field, value in shipped.items():
            assert documented[field] == value, (
                f"[budget].{field} documents {documented[field]!r}; "
                f"the shipped default is {value!r}"
            )

    @classmethod
    def _replace_budget_table(
        cls,
        text: str,
        transform: Callable[[str], str],
    ) -> str:
        start, end = cls._budget_table_span(text)
        table = text[start:end]
        replacement = transform(table)
        assert replacement != table, "control did not change the [budget] table"
        return text[:start] + replacement + text[end:]

    @pytest.mark.parametrize(
        ("command_path", "page", "usage_header"),
        COMMAND_CASES,
    )
    def test_documented_options_equal_shipped_options(
        self,
        command_path: tuple[str, ...],
        page: str,
        usage_header: str,
    ) -> None:
        self._assert_options_match(_read(page), command_path, usage_header)

    def test_command_map_equals_public_registration_tree(self) -> None:
        from typer.main import get_command

        from kew.cli import app

        self._assert_command_coverage(self.COMMAND_CASES, get_command(app))

    def test_command_map_omission_control_is_red_then_restored_green(self) -> None:
        from typer.main import get_command

        from kew.cli import app

        command = get_command(app)
        with pytest.raises(AssertionError, match="differ from public registrations"):
            self._assert_command_coverage(self.COMMAND_CASES[:-1], command)
        self._assert_command_coverage(self.COMMAND_CASES, command)

    def test_unexpected_public_registration_control_is_red_then_restored_green(
        self,
    ) -> None:
        import copy

        import click
        from typer.main import get_command

        from kew.cli import app

        command = copy.copy(get_command(app))
        command.commands = dict(command.commands)
        command.commands["control-only"] = click.Command("control-only")
        with pytest.raises(AssertionError, match="differ from public registrations"):
            self._assert_command_coverage(self.COMMAND_CASES, command)
        self._assert_command_coverage(self.COMMAND_CASES, get_command(app))

    @pytest.mark.parametrize(
        ("command_path", "page", "usage_header", "removed_option"),
        (
            (
                ("review",),
                "docs/cli/review.md",
                "Usage: kew review [OPTIONS] PR_NUMBER",
                "--force-budget",
            ),
            (
                ("model-eol", "check"),
                "docs/cli/model-eol.md",
                "Usage: kew model-eol check [OPTIONS]",
                "-m",
            ),
            (
                ("restore",),
                "docs/cli/restore.md",
                "Usage: kew restore [OPTIONS] ARCHIVE",
                "--no-verify",
            ),
            (
                ("doctor", "route-live-confirm"),
                "docs/cli/doctor.md",
                "Usage: kew doctor route-live-confirm [OPTIONS]",
                "--no-reclaim",
            ),
        ),
    )
    def test_missing_option_control_is_red_then_restored_green(
        self,
        command_path: tuple[str, ...],
        page: str,
        usage_header: str,
        removed_option: str,
    ) -> None:
        original = _read(page)
        variant = self._remove_option(original, usage_header, removed_option)
        with pytest.raises(AssertionError, match="documents"):
            self._assert_options_match(variant, command_path, usage_header)
        self._assert_options_match(original, command_path, usage_header)

    def test_wrapped_description_cannot_replace_option_declaration(self) -> None:
        original = _read("docs/cli/status.md")

        def rewrap_run_description(body: str) -> str:
            description = (
                "  --run TEXT      Show details for a specific run (add --json for "
                "the\n"
                "                  machine-readable work view)"
            )
            rewrapped = (
                "  --run TEXT      Show details for a specific run (add\n"
                "                  --json for the machine-readable work view)"
            )
            assert body.count(description) == 1
            return body.replace(description, rewrapped, 1)

        wrapped_only = self._replace_usage_body(
            original,
            "Usage: kew status [OPTIONS]",
            rewrap_run_description,
        )
        self._assert_options_match(
            wrapped_only,
            ("status",),
            "Usage: kew status [OPTIONS]",
        )

        without_declaration = self._remove_option(
            original,
            "Usage: kew status [OPTIONS]",
            "--json",
        )
        variant = self._replace_usage_body(
            without_declaration,
            "Usage: kew status [OPTIONS]",
            rewrap_run_description,
        )
        with pytest.raises(AssertionError, match="documents"):
            self._assert_options_match(
                variant,
                ("status",),
                "Usage: kew status [OPTIONS]",
            )
        self._assert_options_match(
            original,
            ("status",),
            "Usage: kew status [OPTIONS]",
        )

    @pytest.mark.parametrize(
        ("command_path", "page", "usage_header"),
        (
            (("status",), "docs/cli/status.md", "Usage: kew status [OPTIONS]"),
            (
                ("report", "export"),
                "docs/cli/report.md",
                "Usage: kew report export [OPTIONS]",
            ),
            (("tui",), "docs/tui/index.md", "Usage: kew tui"),
        ),
    )
    def test_unshipped_option_control_is_red_then_restored_green(
        self,
        command_path: tuple[str, ...],
        page: str,
        usage_header: str,
    ) -> None:
        original = _read(page)
        variant = self._add_unshipped_option(original, usage_header)
        with pytest.raises(AssertionError, match="documents"):
            self._assert_options_match(variant, command_path, usage_header)
        self._assert_options_match(original, command_path, usage_header)

    def test_export_short_option_control_is_red_then_restored_green(self) -> None:
        original = _read("docs/cli/export.md")

        def remove_short_option(body: str) -> str:
            assert body.count("-f, --format") == 1
            return body.replace("-f, --format", "--format", 1)

        header = "Usage: kew export [OPTIONS]"
        variant = self._replace_usage_body(original, header, remove_short_option)
        with pytest.raises(AssertionError, match="documents"):
            self._assert_options_match(variant, ("export",), header)
        self._assert_options_match(original, ("export",), header)

    def test_parent_option_cannot_satisfy_child_control(self) -> None:
        original = _read("docs/cli/report.md")
        parent_header = "Usage: kew report [OPTIONS]"
        child_header = "Usage: kew report export [OPTIONS]"
        assert "--output" in self._documented_options(original, parent_header)
        variant = self._remove_option(original, child_header, "--output")
        with pytest.raises(AssertionError, match="documents"):
            self._assert_options_match(
                variant,
                ("report", "export"),
                child_header,
            )
        self._assert_options_match(original, ("report", "export"), child_header)

    def test_usage_header_control_is_red_then_restored_green(self) -> None:
        header = "Usage: kew dispatch [OPTIONS] ISSUE"
        original = _read("docs/cli/dispatch.md")
        variant = original.replace(header, "Usage: kew dispatch ISSUE [OPTIONS]", 1)
        with pytest.raises(AssertionError, match="expected one text usage block"):
            self._assert_options_match(variant, ("dispatch",), header)
        self._assert_options_match(original, ("dispatch",), header)

    def test_serve_no_expose_description_matches_bind_resolver(self) -> None:
        self._assert_serve_no_expose_matches_resolver(_read("docs/cli/serve.md"))

    def test_serve_old_no_expose_claim_is_red_then_restored_green(self) -> None:
        original = _read("docs/cli/serve.md")
        corrected = (
            "  --no-expose       Do not enable CLI exposure; cannot override\n"
            "                    [flight_recorder].expose = true"
        )
        old_claim = "  --no-expose       Refuse a non-loopback bind for this invocation"
        assert original.count(corrected) == 1
        variant = original.replace(corrected, old_claim, 1)
        with pytest.raises(AssertionError, match="configured exposure remains enabled"):
            self._assert_serve_no_expose_matches_resolver(variant)
        self._assert_serve_no_expose_matches_resolver(original)

    def test_audit_policy_usage_matches_click_metadata(self) -> None:
        self._assert_audit_policy_usage_matches_click(_read("docs/cli/audit.md"))

    def test_audit_policy_old_usage_is_red_then_restored_green(self) -> None:
        original = _read("docs/cli/audit.md")
        corrected = "Usage: kew audit policy [OPTIONS] [HASH]"
        old_claim = "Usage: kew audit policy [HASH]"
        assert original.count(corrected) == 1
        variant = original.replace(corrected, old_claim, 1)
        with pytest.raises(AssertionError, match="Click ships"):
            self._assert_audit_policy_usage_matches_click(variant)
        self._assert_audit_policy_usage_matches_click(original)

    def test_configuration_toml_fences_parse(self) -> None:
        text = _read("docs/configuration.md")
        fences = list(self.TOML_FENCE.finditer(text))
        assert fences, "configuration reference has no TOML examples"
        for index, fence in enumerate(fences, start=1):
            try:
                tomllib.loads(fence.group("body"))
            except tomllib.TOMLDecodeError as exc:
                pytest.fail(f"configuration TOML fence {index} does not parse: {exc}")

    def test_complete_configuration_passes_real_validation(self) -> None:
        self._assert_complete_configuration_valid(_read("docs/configuration.md"))

    def test_complete_configuration_restored_true_is_refused(self) -> None:
        from pydantic import ValidationError

        from kew.config import DispatchConfig

        configuration = self._complete_configuration(_read("docs/configuration.md"))
        inference_transport = configuration["inference_transport"]
        assert isinstance(inference_transport, dict)
        switchyard = inference_transport["switchyard"]
        assert isinstance(switchyard, dict)
        sidecar = switchyard["sidecar"]
        assert isinstance(sidecar, dict)
        sidecar["enabled"] = True

        with pytest.raises(ValidationError) as exc_info:
            DispatchConfig.model_validate(configuration)
        reasons = [
            str(error.get("ctx", {}).get("error", error["msg"]))
            for error in exc_info.value.errors()
        ]
        assert reasons == [self.SIDECAR_SWITCHYARD_REFUSAL]

    def test_configuration_sections_equal_the_finite_inventory(self) -> None:
        documented = self._documented_toml_sections(_read("docs/configuration.md"))
        assert documented == self.EXPECTED_TOML_SECTIONS

    def test_documented_toml_values_have_one_explicit_disposition(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        defaults = self._source_default_values(monkeypatch)
        self._assert_toml_coverage_and_defaults(
            _read("docs/configuration.md"),
            defaults,
        )

    def test_commented_values_have_one_explicit_disposition(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        defaults = self._source_default_values(monkeypatch)
        self._assert_commented_toml_coverage(
            _read("docs/configuration.md"),
            defaults,
        )

    def test_default_cells_equal_source_defaults(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        defaults = self._source_default_values(monkeypatch)
        self._assert_default_cells(_read("docs/configuration.md"), defaults)

    def test_repeated_prose_defaults_equal_source_defaults(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        defaults = self._source_default_values(monkeypatch)
        self._assert_prose_defaults(_read("docs/configuration.md"), defaults)

    @pytest.mark.parametrize(
        ("old", "new"),
        (
            (
                "**`combine_with_dispatch`** (default `false`)",
                "**`combine_with_dispatch`** (default `true`)",
            ),
            (
                "**`stall_days`** (default `3`)",
                "**`stall_days`** (default `30`)",
            ),
            (
                'The `overlap_hold` label (default `"overlap-hold"`',
                'The `overlap_hold` label (default `"wrong-label"`',
            ),
        ),
    )
    def test_prose_default_control_is_red_then_restored_green(
        self,
        monkeypatch: pytest.MonkeyPatch,
        old: str,
        new: str,
    ) -> None:
        original = _read("docs/configuration.md")
        assert original.count(old) == 1
        variant = original.replace(old, new, 1)
        defaults = self._source_default_values(monkeypatch)
        with pytest.raises(AssertionError, match="prose default"):
            self._assert_prose_defaults(variant, defaults)
        self._assert_prose_defaults(original, defaults)

    def test_provider_cache_reference_equals_shipped_constants(self) -> None:
        from kew.pricing import PROVIDER_CACHE_DEFAULTS

        section = self._markdown_section(
            _read("docs/configuration.md"),
            "## `[routing.cache]`",
        )
        section = section[section.index("| Provider |") :]
        rows: dict[str, list[str]] = {}
        for line in section.splitlines():
            if not line.startswith("| `"):
                continue
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            rows[cells[0].strip("`")] = cells[1:]

        self._assert_provider_cache_rows(rows, PROVIDER_CACHE_DEFAULTS)

    @staticmethod
    def _assert_provider_cache_rows(rows, PROVIDER_CACHE_DEFAULTS):
        display_names = {
            "anthropic": "anthropic",
            "openai": "openai",
            "google": "google` / Gemini",
        }
        assert set(display_names) == set(PROVIDER_CACHE_DEFAULTS)
        assert set(rows) == {
            "anthropic",
            "openai",
            "google` / Gemini",
            "z.ai`, GLM, unknown",
        }
        for provider, params in PROVIDER_CACHE_DEFAULTS.items():
            TestMigrationDocumentationDrift._assert_provider_cache_row(
                rows[display_names[provider]], params
            )

        assert rows["z.ai`, GLM, unknown"][0] == "**no**"

    @staticmethod
    def _assert_provider_cache_row(row, params):
        caches, read, write, ttl, minimum, style = row
        assert caches == ("yes" if params.caches else "no")
        assert f"{params.read_mult:.2f}×" in read
        assert f"{params.write_mult}×" in write
        assert ttl == f"{params.ttl_seconds}s"
        assert minimum == f"{params.min_tokens:,}"
        assert style.replace(" ", "") == params.style

    def test_removed_configuration_keys_remain_explanations(self) -> None:
        text = _read("docs/configuration.md")
        removed_explanations = (
            "legacy `[dispatch]` section is removed",
            "removed\n`brand_name`, `brand_logo`, and `brand_accent` keys",
            "**`on_missing_runtime`** — **removed.**",
        )
        for explanation in removed_explanations:
            assert text.count(explanation) == 1

    def test_intentional_examples_are_labeled(self) -> None:
        text = _read("docs/configuration.md")
        labels = (
            'name = "my-project"           # Intentional example',
            "max_budget_usd = 10.0         # Intentional example",
            "intentional inactive transport example",
            "Intentional examples: replace these patterns",
            "Intentional activated examples; threshold_usd",
            "Intentional example: use different full and inner-loop gates",
            "Intentional routing example",
            "Intentional activated examples.",
            "Intentional runner-image mapping example",
            "Intentional runner-provider mapping example.",
            "Intentional per-runner image override examples.",
            'simple = { runner = "opencode", '
            'model = "openrouter/google/gemini-3.8-flash" } # Intentional example',
            '# medium = "claude-code"                                      '
            "# Intentional example",
            'complex = { runner = "claude-code", model = "claude-opus-4-6" } '
            "# Intentional example",
            'overrides = { "custom-model" = [3.0, 15.0] } # Intentional example',
            'approvers = ["octocat"]     # Intentional example',
            'approvers = ["alice"]     # Intentional example',
        )
        for label in labels:
            assert text.count(label) == 1

    def test_section_deletion_control_is_red_then_restored_green(self) -> None:
        original = _read("docs/configuration.md")
        variant = original.replace("[flight_recorder]", "[flight_recorder_removed]")
        with pytest.raises(AssertionError):
            assert (
                self._documented_toml_sections(variant) == self.EXPECTED_TOML_SECTIONS
            )
        assert self._documented_toml_sections(original) == self.EXPECTED_TOML_SECTIONS

    def test_duplicate_default_is_checked_in_its_own_section(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original = _read("docs/configuration.md")
        declaration = (
            'host = "127.0.0.1"            # receiver bind host; loopback-only'
        )
        positions = [
            match.start() for match in re.finditer(re.escape(declaration), original)
        ]
        assert len(positions) == 2
        second = positions[1]
        variant = original[:second] + original[second:].replace(
            declaration,
            'host = "127.0.0.2"            # receiver bind host; loopback-only',
            1,
        )
        defaults = self._source_default_values(monkeypatch)
        with pytest.raises(AssertionError, match="ingest.host documents"):
            self._assert_toml_coverage_and_defaults(variant, defaults)
        self._assert_toml_coverage_and_defaults(original, defaults)

    def test_intentional_example_control_is_red_then_restored_green(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original = _read("docs/configuration.md")
        variant = original.replace("max_budget_usd = 10.0", "max_budget_usd = 25.0", 1)
        defaults = self._source_default_values(monkeypatch)
        with pytest.raises(AssertionError, match="intentional example"):
            self._assert_toml_coverage_and_defaults(variant, defaults)
        self._assert_toml_coverage_and_defaults(original, defaults)

    @pytest.mark.parametrize(
        ("declaration", "replacement", "path"),
        (
            (
                '    claude-code = "kew-agent:docker-d148816b388f"',
                '    claude-code = "kew-agent:changed"',
                "sandbox.images.claude-code",
            ),
            (
                '    opencode = "kew-agent-opencode:docker-d148816b388f"',
                '    opencode = "kew-agent-opencode:changed"',
                "sandbox.images.opencode",
            ),
            (
                '    codex = "kew-agent-codex:docker-d148816b388f"',
                '    codex = "kew-agent-codex:changed"',
                "sandbox.images.codex",
            ),
        ),
    )
    def test_indented_intentional_image_control_is_red_then_restored_green(
        self,
        monkeypatch: pytest.MonkeyPatch,
        declaration: str,
        replacement: str,
        path: str,
    ) -> None:
        original = _read("docs/configuration.md")
        assert original.count(declaration) == 1
        variant = original.replace(declaration, replacement, 1)
        defaults = self._source_default_values(monkeypatch)
        with pytest.raises(
            AssertionError,
            match=rf"intentional example {re.escape(path)}",
        ):
            self._assert_toml_coverage_and_defaults(variant, defaults)
        self._assert_toml_coverage_and_defaults(original, defaults)

    def test_commands_verify_raw_default_and_effective_fallback(self) -> None:
        from kew.config import DispatchConfig
        from kew.prompt import build_template_context

        config = DispatchConfig()
        context = build_template_context(
            issue_num=1,
            issue_title="title",
            issue_body="body",
            complexity="simple",
            config=config,
            has_coderabbit=False,
        )
        assert config.commands.verify == ""
        assert context["commands"]["verify"] == config.commands.ci

    def test_documented_budget_defaults_equal_shipped_defaults(self) -> None:
        self._assert_budget_matches(_read("docs/configuration.md"))

    @pytest.mark.parametrize(
        "field",
        ("per_issue_usd", "daily_usd", "weekly_usd"),
    )
    def test_wrong_budget_default_control_is_red_then_restored_green(
        self,
        field: str,
    ) -> None:
        from kew.config import BudgetConfig

        original = _read("docs/configuration.md")
        shipped_value = BudgetConfig().model_dump()[field]

        def change_value(table: str) -> str:
            pattern = re.compile(
                rf"^({re.escape(field)}\s*=\s*){re.escape(str(shipped_value))}"
                r"(?P<suffix>\s*(?:#.*)?)$",
                re.MULTILINE,
            )
            replacement, count = pattern.subn(
                lambda match: (
                    f"{match.group(1)}{shipped_value + 1.0}{match.group('suffix')}"
                ),
                table,
            )
            assert count == 1, f"expected one documented value for {field}"
            return replacement

        variant = self._replace_budget_table(original, change_value)
        variant += f"\n<!-- Original control value: {shipped_value}. -->\n"
        with pytest.raises(AssertionError, match=rf"\[budget\]\.{field} documents"):
            self._assert_budget_matches(variant)
        self._assert_budget_matches(original)

    def test_missing_budget_key_control_is_red_then_restored_green(self) -> None:
        original = _read("docs/configuration.md")

        def remove_key(table: str) -> str:
            replacement, count = re.subn(
                r"^daily_usd\s*=.*\n",
                "",
                table,
                count=1,
                flags=re.MULTILINE,
            )
            assert count == 1
            return replacement

        variant = self._replace_budget_table(original, remove_key)
        with pytest.raises(AssertionError, match="documented .* keys"):
            self._assert_budget_matches(variant)
        self._assert_budget_matches(original)

    def test_extra_budget_key_control_is_red_then_restored_green(self) -> None:
        original = _read("docs/configuration.md")

        def add_key(table: str) -> str:
            return table.rstrip() + "\nmonthly_usd = 400.0\n\n"

        variant = self._replace_budget_table(original, add_key)
        with pytest.raises(AssertionError, match="documented .* keys"):
            self._assert_budget_matches(variant)
        self._assert_budget_matches(original)


class TestFrontDoorTruthfulness:
    INSTALL = (
        'uv tool install "kew-cli[serve] @ '
        'git+https://github.com/thossullivan/kew.git@main#subdirectory=kew"'
    )

    def test_evidence_follows_the_hero_and_quotes_consistent_pack_numbers(
        self,
    ) -> None:
        page = _read("www/src/routes/+page.svelte")
        # Evidence takes the hashes the page's load computes at build time as
        # a prop, so it carries attributes; the order holds.
        assert re.search(r"<Hero\s*/>\s*<Evidence\b[^>]*/>\s*<Loop\b[^>]*/>", page)

        evidence = _read("www/src/lib/Evidence.svelte")
        # The pack file lives in the private archive; no page may link to it.
        assert _PACKS not in evidence
        assert not (REPO_ROOT / _PACKS).exists()
        assert "Every engagement ends" not in evidence
        assert "kew report export" in evidence

        pack = _read("www/src/lib/data/pack-figures.js")
        self._assert_launch_pack_economics(pack)

    @staticmethod
    def _assert_launch_pack_economics(pack):
        def number(key: str) -> float:
            found = re.search(rf"^\s*{key}: ([0-9.]+),$", pack, re.M)
            assert found is not None, key
            return float(found.group(1))

        calculated = number("totalSpend") / number("prsMerged")
        assert number("costPerMergedPr") == pytest.approx(calculated)
        assert f"${calculated:.2f}" == "$6.70"

    def test_install_copy_uses_the_source_command(self) -> None:
        hero = _read("www/src/lib/Hero.svelte")
        install = _read("www/src/lib/Install.svelte")
        command_source = _read("www/src/lib/data/install.js")
        # The install section prints the command in INSTALL_PARTS, its
        # line-break pieces, which are split from INSTALL and so join back to
        # it exactly. The hero prints no copy of it: its primary action links
        # to that section (#3354, W4).
        install_import = (
            "import { INSTALL, INSTALL_PARTS, REPO_URL } from './data/install';"
        )
        # The slug is written once; the command is built from it.
        assert "export const REPO_SLUG = 'thossullivan/kew';" in command_source
        assert "export const REPO_URL = `https://github.com/${REPO_SLUG}`;" in (
            command_source
        )
        template = self.INSTALL.replace(
            "https://github.com/thossullivan/kew", "${REPO_URL}"
        )
        assert f"export const INSTALL = `{template}`;" in command_source
        assert (
            "export const INSTALL_PARTS = INSTALL.split(/(?<=@)|(?=#subdirectory)/);"
            in command_source
        )
        assert '<a class="cta primary" href="#install">Install kew</a>' in hero
        assert "INSTALL_PARTS" not in hero
        assert install_import in install
        render = "{#each INSTALL_PARTS as part, i}{#if i > 0}<wbr />{/if}{part}{/each}"
        assert render in install
        assert "navigator.clipboard.writeText(INSTALL)" in install
        for page in (hero, install):
            assert "git+https" not in page
            assert "pip install" not in page
        assert self.INSTALL not in hero
        assert self.INSTALL not in install
        assert "pip install kew-cli" not in command_source
        assert "Sixty seconds" not in install
        assert '<p class="eyebrow">Install from source</p>' in install

    def test_isolation_copy_matches_the_product_contract(self) -> None:
        loop = _read("www/src/lib/Loop.svelte")
        assert "standalone clone" in loop
        assert "git worktree" not in loop


class TestExperimentalMarkers:
    """Rulings 2, 3 and 6 (2026-08-24/26): Switchyard, Lemonade and Linear are
    labelled experimental everywhere the public sees them."""

    def test_experimental_banners_on_the_feature_pages(self) -> None:
        for page in ("docs/cli/switchyard.md", "docs/runners/lemonade.md"):
            assert '!!! warning "Experimental"' in _read(page), page

    def test_readme_marks_every_experimental_feature(self) -> None:
        readme = _read("README.md")
        assert "**`lemonade`** *(experimental)*" in readme
        assert "The Linear source is also **experimental**" in readme
        runner_table = readme.split("## Runners", 1)[1].split("### Experimental", 1)[0]
        assert "Switchyard" not in runner_table
        experimental = readme.split("### Experimental", 1)[1].split(
            "## Quick Start", 1
        )[0]
        assert experimental.count("Switchyard") == 1
        assert "docs/cli/switchyard.md" in experimental

    def test_runners_index_marks_switchyard_and_lemonade(self) -> None:
        index = _read("docs/runners/index.md")
        assert "**`lemonade`** *(experimental)*" in index
        assert "Switchyard through OpenAI Responses *(experimental)*" in index


class TestLoopVerificationDocs:
    """Loop verification documentation matches contained and admitted host paths."""

    PAGES = ("docs/cli/loop.md", "docs/security.md", "docs/threat-model.md")

    def test_pages_name_the_gate_and_the_finding_and_invent_no_sandbox(self) -> None:
        for page in self.PAGES:
            text = _read(page)
            assert "kew.loops.verification.CommandCondition.check" in text, page
            assert "#1444" in text, page
            assert "loop sandbox" not in text, page

    def test_host_fallback_really_is_sh_dash_c(self) -> None:
        from kew.loops.verification import CommandCondition

        assert callable(CommandCondition.check)
        cmd = CommandCondition("true")._build_cmd()[0]
        assert cmd[:2] == ["sh", "-c"]

    def test_pages_list_every_default_guarded_path(self) -> None:
        from kew.loops.verification import DEFAULT_GUARDED_PATHS

        for page in ("docs/cli/loop.md", "docs/security.md"):
            text = _read(page)
            for glob in DEFAULT_GUARDED_PATHS:
                assert f"`{glob}`" in text, (page, glob)

    def test_dispatch_documents_loop_verification_admission(self) -> None:
        text = _read("docs/cli/dispatch.md")
        assert "[sandbox] dangerously_skip_containment = true" in text
        assert "#1444" in text
        assert "loop.md" in text

    def test_loop_pages_document_new_dispatch_admission_order(self) -> None:
        for page in ("docs/cli/dispatch.md", "docs/cli/loop.md", "docs/security.md"):
            text = " ".join(_read(page).split())
            assert "select the issue, resolve the route, and save" in text, page
            assert "before worktree setup or subprocess execution" in text, page
            assert "before route resolution or worktree" not in text.lower(), page

    def test_loop_pages_document_external_host_verification_mitigation(self) -> None:
        for page in ("docs/cli/loop.md", "docs/security.md"):
            text = " ".join(_read(page).split())
            assert "OS-level sandbox" in text, page
            assert "dedicated unprivileged account" in text, page


# ---------------------------------------------------------------------------
# docs/cli/model-eol.md "Current coverage" states a rule by hand (#2709) and
# carries a generated list (#2710). The page once listed the packaged registry's
# ids by hand under a count word, and the count went stale whenever a row landed
# (#1842 review). Now the list and its count are rendered from the model catalog
# between marker comments (tests/test_model_catalog_docs.py holds them to the
# generator); the hand-written text around them names no id and pins no count.
# ---------------------------------------------------------------------------

_COVERAGE_HEADING = "## Current coverage"


def _lifecycle_coverage_section(text: str) -> str:
    assert text.count(_COVERAGE_HEADING) == 1, (
        f"docs/cli/model-eol.md must have exactly one {_COVERAGE_HEADING!r} heading"
    )
    start = text.index(_COVERAGE_HEADING)
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


_MODEL_LIST_BLOCK = re.compile(
    r"<!-- kew:model-list:begin\b.*?<!-- kew:model-list:end -->", re.DOTALL
)


def test_model_eol_page_hand_text_states_the_rule_without_a_list() -> None:
    from kew.model_catalog import load_catalog

    section = _lifecycle_coverage_section(_read("docs/cli/model-eol.md"))
    assert len(_MODEL_LIST_BLOCK.findall(section)) == 1, (
        "the coverage section must hold the one generated model list"
    )
    hand_text = _MODEL_LIST_BLOCK.sub("", section)
    assert "kew/src/kew/models.json" in hand_text
    named = [row.id for row in load_catalog().rows if f"`{row.id}`" in hand_text]
    assert not named, f"hand-written coverage text must not list model ids: {named}"
    assert not re.search(r"\b(?:exactly|covers) the \w+\b", hand_text), (
        "hand-written coverage text must not pin a row count"
    )


def test_a_catalog_lifecycle_claim_carries_evidence() -> None:
    """Only ``untracked`` may stand without evidence: any other status is a claim."""
    from kew.model_catalog import load_catalog

    for row in load_catalog().rows:
        if row.lifecycle.status != "untracked":
            assert row.lifecycle.evidence, row.id


def test_epic_abandon_documents_recovery_evidence_and_exit_codes():
    page = _read("docs/cli/epic.md")
    assert "Usage: kew epic abandon [OPTIONS] NAME NODE" in page
    for token in (
        "continuation_released",
        "continuation_abandoned",
        "legacy evidence gaps",
        "without launched runs",
        "execution evidence remain publication ancestors",
        "KEW_ACTOR",
        "[epic].approvers",
        "[approval].approvers",
        "**0**",
        "**1**",
        "**2**",
        "required",
        "non-whitespace",
        "retry marker",
        "copy any wanted edits",
        "Fresh dispatch refuses active continuations across epics",
        "matching checkout withdrawal evidence and cleared holds",
        "even before run retention",
        "Existing audit rows and run history remain unchanged",
        "abandonment can rebind only to the same item and branch",
        "`rebound_from` with the original recorded path",
        '`rebound_reason` as `"recorded checkout missing"`',
        "An attempt that fails before expiry leaves abandonment available",
        "resume does not reopen them",
    ):
        assert token in page


# Exit labels are exact fields, so moving a label to another row must fail.
_ROUND_THREE_EXIT_ROWS = {
    "run": {
        "empty queue": 0,
        "GitHub query failure": 2,
        "configuration failure": 2,
        "potentially stranded fleet claim": 2,
    },
    "status": {"Unknown run": 2},
    "restore": {"Schema too new": 2, "database locked by another process": 2},
    "ci-fix": {
        "PR lookup query failure": 2,
        "unavailable CI logs": 2,
        "preparation failure": 2,
    },
    "review": {"pre-launch refusal (not_run)": 2, "Runner or publication failure": 1},
    "resolve": {"pre-launch refusal (not_run)": 2, "Runner or publication failure": 1},
    "doctor": {
        "Boundary confirmed live": 0,
        "Completed probe refuted the boundary": 1,
        "Probe refused": 2,
        "spend confirmation declined": 2,
    },
}


def _assert_round_three_exit_rows(page: str, text: str) -> None:
    import kew.cli as cli

    # Require the execution-state boundary before binding its published rows.
    boundary = vars(cli).get("_command_exit_code")
    assert callable(boundary), "review/resolve require an execution-state boundary"
    from types import SimpleNamespace

    shipped = {
        0: cli.SUCCESS_EXIT_CODE,
        1: boundary(SimpleNamespace(kind="executed", exit_code=127)),
        2: cli.COULD_NOT_RUN_EXIT_CODE,
    }
    if page in ("review", "resolve"):
        shipped[2] = boundary(SimpleNamespace(kind="not_run", exit_code=1))
    if page == "ci-fix":
        assert cli.CI_FIX_QUERY_FAILED not in (0, 1, 2)
        shipped[2] = cli.GH_QUERY_FAILURE_EXIT_CODE
    rows = re.findall(r"^\| `(\d+)` \| ([^|]+) \|$", text, re.MULTILINE)
    for label, scheme_code in _ROUND_THREE_EXIT_ROWS[page].items():
        codes = [
            int(code) for code, cell in rows if label in cell.rstrip(".").split("; ")
        ]
        assert codes == [shipped[scheme_code]], f"{page}: {label} documents {codes}"
    assert not any(int(code) > 2 for code, _cell in rows)


@pytest.mark.parametrize("page", _ROUND_THREE_EXIT_ROWS)
def test_round_three_exit_reference_rows_match_constants(page):
    text = _read(f"docs/cli/{page}.md")
    _assert_round_three_exit_rows(page, text)
    assert "(exit-codes.md)" in text
    if page == "status":
        assert "stderr and exit code 2" in text
        assert "stderr and exit code 1" not in text


@pytest.mark.parametrize("page", _ROUND_THREE_EXIT_ROWS)
def test_round_three_stale_exit_row_fails(page):
    text = _read(f"docs/cli/{page}.md")
    _assert_round_three_exit_rows(page, text)
    label, code = next(iter(_ROUND_THREE_EXIT_ROWS[page].items()))
    row = next(
        line
        for line in text.splitlines()
        if line.startswith(f"| `{code}` |") and label in line
    )
    stale = text.replace(row, row.replace(f"`{code}`", "`3`", 1), 1)
    with pytest.raises(AssertionError, match=page):
        _assert_round_three_exit_rows(page, stale)


_ROUND_FOUR_REFUSALS = {
    "loop": ("Loop operation refusal", ["loop", "pause", "missing"]),
    "serve": ("Unsafe bind refusal", ["serve", "--host", "0.0.0.0"]),
    "approve": ("Authorization refusal", ["approve", "42"]),
    "deny": ("Authorization refusal", ["deny", "42"]),
}


def _assert_bound_refusals(text, observed):
    rows = {}
    for family, cells in re.findall(
        r"^\| `(loop|serve|approve|deny)` \| (.+) \|$", text, re.MULTILINE
    ):
        assert family not in rows, f"duplicate refusal family: {family}"
        rows[family] = cells.split(" | ")
    assert rows.keys() == _ROUND_FOUR_REFUSALS.keys()
    for family, cells in rows.items():
        label, _command = _ROUND_FOUR_REFUSALS[family]
        refusals = [
            (code, item)
            for code, cell in enumerate(cells)
            for item in cell.split("; ")
            if re.search(r"refus", item, re.IGNORECASE)
        ]
        assert refusals == [(observed[family], label)], (
            f"{family}: unbound or incorrect refusal row: {refusals}"
        )


@pytest.mark.parametrize("family", _ROUND_FOUR_REFUSALS)
def test_round_four_refusal_rows_match_executed_commands(family, monkeypatch):
    from unittest.mock import MagicMock

    from typer.testing import CliRunner

    from kew.cli import app
    from kew.config import DispatchConfig

    monkeypatch.setattr("kew.cli.load_config", lambda **kwargs: DispatchConfig())
    monkeypatch.setattr("kew.cli.preflight_tools", lambda: None)
    monkeypatch.setattr("kew.cli._open_audit_store", lambda config: None)
    monkeypatch.setattr(
        "kew.approval.apply_approval_decision", lambda *a, **k: "unauthorized"
    )
    store = MagicMock()
    store.get_loop.return_value = None
    store.request_pause.return_value = False
    monkeypatch.setattr("kew.cli._open_run_store", lambda *a, **k: store)
    store.__enter__.return_value = store
    result = CliRunner().invoke(app, _ROUND_FOUR_REFUSALS[family][1])
    assert result.exit_code == 2, result.output
    expected = {
        "loop": "not running",
        "serve": "non-loopback",
        "approve": "not authorized",
        "deny": "not authorized",
    }
    assert expected[family] in result.output.lower()
    observed = dict.fromkeys(_ROUND_FOUR_REFUSALS, 2)
    observed[family] = result.exit_code
    text = _read("docs/cli/exit-codes.md")
    _assert_bound_refusals(text, observed)
    # Every migrated refusal must have a command binding; a new unbound claim fails.
    stale = text.replace(f"| `{family}` |", f"| `{family}` | Unbound refusal; ", 1)
    with pytest.raises(AssertionError, match=family):
        _assert_bound_refusals(stale, observed)


@pytest.mark.parametrize(
    "status,launched,label,expected",
    [
        ("achieved", True, "Resume completed", 0),
        ("needs_human", True, "Resume needs human or failed verification", 1),
        ("budget_limited", True, "Resume budget limited", 1),
        ("needs_human", False, "Resume prerequisite or query failure", 2),
    ],
)
def test_round_five_resume_family_binding(
    monkeypatch, tmp_path, status, launched, label, expected
):
    from test_cli_loop import _round_five_resume_setup
    from typer.testing import CliRunner

    from kew.cli import app

    _round_five_resume_setup(monkeypatch, tmp_path)

    def resume(self, loop, guidance, **kwargs):
        self.launched = launched
        loop.status = status
        return loop

    monkeypatch.setattr("kew.loops.delegate.DelegateDriver.resume", resume)
    result = CliRunner().invoke(
        app, ["loop", "resume", "resume-test", "--guidance", "go"]
    )
    row = re.search(
        r"^\| `loop` \| (.+) \|$", _read("docs/cli/exit-codes.md"), re.MULTILINE
    )
    assert row is not None
    cells = row.group(1).split(" | ")
    matches = [code for code, cell in enumerate(cells) if label in cell.split("; ")]
    assert matches == [result.exit_code] == [expected]
    # Moving the label to another column breaks the same binding.
    cells[expected] = cells[expected].replace(label, "")
    cells[(expected + 1) % 3] += "; " + label
    moved = [code for code, cell in enumerate(cells) if label in cell.split("; ")]
    assert moved != [result.exit_code]


def test_round_six_sandbox_smoke_documented_codes_match_execution(monkeypatch):
    from typer.testing import CliRunner

    import kew.cli as cli
    import kew.sandbox as sandbox
    from kew.config import DispatchConfig

    monkeypatch.setattr(cli, "load_config", DispatchConfig)
    observed = {}
    for layer in ("runtime", "image", "launch", "execution", "auth", "success"):
        smoke = sandbox.SmokeResult(ok=layer == "success", layer=layer, detail="test")
        monkeypatch.setattr(
            sandbox, "run_smoke", lambda *args, _result=smoke, **kwargs: _result
        )
        result = CliRunner().invoke(cli.app, ["sandbox", "smoke"])
        observed[layer] = result.exit_code
        if layer != "success":
            assert f"[{layer}]" in result.output
    text = _read("docs/cli/sandbox.md").split("## kew sandbox smoke", 1)[1]

    def documented(page):
        rows = re.findall(r"^\| `(\d+)` \| (.+) \|$", page, re.MULTILINE)
        return {
            layer: [int(code) for code, description in rows if layer in description]
            for layer in observed
        }

    assert documented(text) == {layer: [code] for layer, code in observed.items()}
    assert observed == {
        "runtime": 2,
        "image": 1,
        "launch": 1,
        "execution": 1,
        "auth": 1,
        "success": 0,
    }
    stale = text.replace("| `2` |", "| `1` |")
    assert documented(stale) != {layer: [code] for layer, code in observed.items()}


def test_round_seven_sandbox_build_rows_match_cli(monkeypatch):
    import subprocess

    from typer.testing import CliRunner

    import kew.cli as cli
    import kew.sandbox as sandbox
    from kew.config import DispatchConfig

    monkeypatch.setattr(cli, "load_config", DispatchConfig)
    observed = {}
    labels = {
        "failed": "Build ran and failed",
        "verification": "Built image verification failed",
        "missing": "Docker missing",
        "daemon": "Docker daemon unreachable",
        "success": "Image built",
    }
    for situation, expected in (
        ("failed", 1),
        ("verification", 1),
        ("missing", 2),
        ("daemon", 2),
        ("success", 0),
    ):
        monkeypatch.setattr(
            sandbox,
            "detect_runtime",
            lambda _config, s=situation: None if s == "missing" else "/mock/docker",
        )
        calls = []

        def runtime(argv, *, calls=calls, situation=situation, **kwargs):
            calls.append(argv)
            if "info" in argv and situation == "daemon":
                return subprocess.CompletedProcess(
                    argv, 1, "", "Cannot connect to the Docker daemon"
                )
            if "build" in argv:
                code = 17 if situation in {"failed", "daemon"} else 0
                return subprocess.CompletedProcess(
                    argv, code, "build step failed" if code else "", ""
                )
            return subprocess.CompletedProcess(argv, 0, "{}", "")

        monkeypatch.setattr(sandbox.subprocess_utils, "run", runtime)
        monkeypatch.setattr(sandbox, "allowlist_network_exists", lambda *args: True)
        resolution = sandbox.ImageResolution(
            sandbox.ImageIdStatus.UNAVAILABLE
            if situation == "verification"
            else sandbox.ImageIdStatus.RESOLVED,
            image_id=None if situation == "verification" else "sha256:" + "a" * 64,
        )
        monkeypatch.setattr(
            sandbox, "inspect_image_id", lambda *args, resolution=resolution: resolution
        )
        result = CliRunner().invoke(cli.app, ["sandbox", "build"])
        observed[situation] = result.exit_code
        assert result.exit_code == expected, (situation, result.output)
        assert any("build" in call for call in calls) is (
            situation not in {"missing", "daemon"}
        )

    def bind(family_page, sandbox_page):
        row = re.search(r"^\| `sandbox` \| (.+) \|$", family_page, re.MULTILINE)
        assert row is not None
        cells = row.group(1).split(" | ")
        rows = re.findall(
            r"^\| `(\d+)` \| (.+) \|$",
            sandbox_page.split("## kew sandbox smoke")[0],
            re.MULTILINE,
        )
        for situation, code in observed.items():
            label = labels[situation]
            assert [i for i, cell in enumerate(cells) if label in cell.split("; ")] == [
                code
            ], label
            assert [int(i) for i, cell in rows if label in cell] == [code], label

    family_page = _read("docs/cli/exit-codes.md")
    sandbox_page = _read("docs/cli/sandbox.md")
    bind(family_page, sandbox_page)
    with pytest.raises(AssertionError, match="Build ran and failed"):
        bind(
            family_page.replace("Build ran and failed", "Stale build row"), sandbox_page
        )
    with pytest.raises(AssertionError, match="Build ran and failed"):
        bind(
            family_page, sandbox_page.replace("| `1` | Build ran", "| `2` | Build ran")
        )


def test_bounded_issuer_docs_keep_route_selective_claims() -> None:
    security = _read("docs/security.md")
    rows = [line.split("|") for line in security.splitlines() if line.startswith("| ")]
    issuer_row = next(
        row for row in rows if "host `OPENROUTER_PROVISIONING_KEY`" in row[1]
    )
    assert issuer_row[3].strip().startswith("Yes — per-run key")
    for label in (
        "Contained claude-code",
        "Native Codex",
        "Direct OpenRouter without",
        "Switchyard sidecar",
    ):
        row = next(row for row in rows if label in row[1])
        assert row[3].strip() == "No"
    for path in ("docs/security.md", "docs/cli/sandbox.md"):
        doc = _read(path)
        for claim in (
            "OPENROUTER_PROVISIONING_KEY",
            "limit_reset: null",
            "300 seconds",
            "daily remaining budget",
            "no provisioning key",
            "issued:openrouter:<hash>",
            "credential_issuer.revoked",
        ):
            assert claim in doc

    for path in (
        "docs/authentication.md",
        "docs/sandbox-entrypoint-contract.md",
        "docs/runners/openrouter.md",
    ):
        assert "OPENROUTER_PROVISIONING_KEY" in _read(path)


def test_bounded_issuer_docs_pin_complete_retry_lifetime() -> None:
    for path in ("docs/security.md", "docs/cli/sandbox.md"):
        doc = _read(path)
        assert "attempts * timeout + 1.25 * sum(backoff intervals) + 300 seconds" in doc
        assert (
            "refuses launch when the remaining retry schedule exceeds "
            "the remaining key lifetime" in doc
        )
        assert "Retries share one key and one total spend ceiling." in doc


@pytest.mark.parametrize(
    "page", ["docs/threat-model.md", "docs/security.md", "docs/configuration.md"]
)
def test_uncontained_flows_policy_claim(page):
    text = " ".join(_read(page).split())
    assert "`allow_uncontained_flows = true`" in text
    assert (
        "With `enabled = true`, review remediation and conflict resolution "
        "fail closed when containment prerequisites are missing." in text
    )
    assert (
        "runtime, runner image mapping, and declared credential in the environment"
        in text
    )
    assert "unset and `false` permit no exception" in text
    assert 'Auto fallback records `admission = "prerequisite_gap"`' in text
    assert 'explicit `enabled = false` retains `admission = "policy"`' in text
    assert (
        "CI-fix retains its stricter dispatch gate and exit code 2 (could not run)"
        in text
    )
    assert (
        "Review and resolve containment refusals return exit code 2 (could not run)"
        in text
    )
    if page == "docs/configuration.md":
        assert (
            "With auto or disabled containment, `true` is accepted as a no-op" in text
        )
        assert "`kew config effective --sources` reports the no-op" in text


@pytest.mark.parametrize("page", ["docs/security.md", "docs/cli/loop.md"])
def test_loop_containment_claims_bind_to_execution(page):
    from kew import sandbox
    from kew.config import CommandsConfig
    from kew.loops import verification

    text = " ".join(_read(page).split())
    assert "Command verification runs contained when prerequisites are present." in text
    assert (
        "The host path requires `[sandbox] dangerously_skip_containment = true` "
        "and missing prerequisites." in text
    )
    assert "Canonical `ci_green` needs no local execution" in text
    assert "verify_loop_needs_writes = true" in text
    assert "600" in text and "--network none" in text
    assert "contained_verification" in text and "host_verification" in text
    assert CommandsConfig().verify_loop_needs_writes is False
    assert verification.VERIFICATION_TIMEOUT_SECONDS == 600
    assert (
        verification.build_checkout_program_argv is sandbox.build_checkout_program_argv
    )


def test_publication_intent_docs_define_scopes_and_producer_ids():
    doc = _read("docs/security.md")
    for claim in (
        'scope = "item"',
        'scope = "branch"',
        "PR head branch",
        "The GitHub Actions run ID never identifies the publication producer.",
        "Clean integrations reuse the retained producer run "
        "because they launch no agent.",
        "Older intents without `scope` retain item scope.",
    ):
        assert claim in doc


def test_round14_checkout_identity_documents_aliases_and_missing_paths():
    text = _read("docs/cli/epic.md")
    assert ("Filesystem identity prevents alias or relocation bypasses.") in text
    assert (
        "Missing-path ownership applies only to the same item and branch, across epics."
    ) in text


def test_round15_checkout_identity_documents_logical_scope_and_completion():
    text = _read("docs/cli/epic.md")
    assert (
        "Missing-path ownership applies only to the same item and branch, across epics."
        in text
    )
    assert "A completed continuation never blocks another item." in text
    assert "Resume also accepts terminal continuations without proven success" in text


def test_round19_loop_dispatch_documents_abandonment_consumption():
    text = _read("docs/cli/epic.md")
    assert (
        "Loop dispatch consumes abandonment authority like fresh dispatch, "
        "before its first step launches."
    ) in text
    assert "Loop setup requires a run store and run identity" in text
    assert "without reopening consumed abandonment" in text


def test_round20_generation_sidecar_and_loop_boundaries_documented():
    epic = _read("docs/cli/epic.md")
    assert "`token`, `device`, and `inode`" in epic
    assert "32 lowercase hexadecimal characters" in epic
    assert "revoke_checkout_generation" in epic
    assert "kew epic abandon" in epic
    assert "Every paid attempt, including retries" in epic
    assert "Host publication validates the same binding again" in epic
    assert (
        "Refusal before the first worker launch still permits replacement withdrawal"
        in epic
    )


def test_round21_generation_factors_and_legacy_retirement_documented():
    for path in ("docs/cli/epic.md",):
        text = _read(path)
        for term in (
            "`birth_field`",
            "`birth_time`",
            "`st_birthtime`",
            "`.git/kew-generation`",
            "legacy_sidecar_retired=true",
            "validated abandonment authority",
        ):
            assert term in text, (path, term)


def test_round22_generation_binding_modes_documented():
    for path in ("docs/cli/epic.md",):
        text = _read(path)
        for statement in (
            "Birth binding has two modes.",
            "With `st_birthtime`, reads validate device, inode, birth identity, "
            "and the in-checkout nonce.",
            "Without an immutable birth identity, the sidecar records "
            '`birth_field` as `"none"` and omits `birth_time`.',
            "This mode validates device, inode, and the in-checkout nonce.",
            "The nonce inside the checkout provides a consistency check, "
            "never identity evidence.",
            "Reads ignore a recorded birth field "
            "when the current platform cannot read it.",
            "Those reads still validate device, inode, and the in-checkout nonce.",
            "Mutable directory timestamps never bind a generation.",
            "Creating, deleting, or renaming checkout root files "
            "preserves the generation.",
        ):
            assert statement in text, (path, statement)
        assert "earliest of `st_ctime` and `st_mtime`" not in text
        assert "changed timestamp requires recovery" not in text


def test_round26_checkout_binding_threat_model():
    for path in ("docs/threat-model.md", "docs/security.md"):
        text = _read(path)
        for statement in (
            "Checkout binding defends against replacement by another process, "
            "including concurrent dispatch, resume, and operator commands.",
            "It also defends against a contained agent, "
            "which cannot write outside its mount.",
            "A host agent runs with the operator's privileges "
            "and can also edit the ledger.",
            "A host agent is outside this boundary. "
            "Containment is the defense for that case.",
            "Each boundary compares `os.fstat(fd)` and `os.stat(path)` "
            "with the persisted evidence.",
            "No agent-writable file authorizes checkout identity.",
        ):
            assert statement in text, (path, statement)


def test_round_ten_startup_acknowledgement_and_unknown_exit_contract():
    root = Path(__file__).resolve().parents[2]
    contract = (root / "docs/sandbox-entrypoint-contract.md").read_text()
    exits = (root / "docs/cli/exit-codes.md").read_text()
    assert "KEW_AGENT_START_NONCE" in contract
    assert "KEW_AGENT_START <KEW_AGENT_START_NONCE>" in contract
    assert "Generic JSON lines never establish startup" in contract
    assert "never enters the events file" in contract
    assert (
        "| Stream failure with missing acknowledgement: UNKNOWN | `1` | Forbidden |"
        in exits
    )
    assert (
        "| Completed wrapper without acknowledgement; no earlier execution, "
        "including raw exit `0` "
        "| `2` | Allowed |" in exits
    )
    assert (
        "| Final attempt refused after earlier confirmed or UNKNOWN execution, "
        "including raw exit `0` "
        "| `1` | Forbidden |" in exits
    )
    assert "UNKNOWN execution also returns `1` on failure." in exits


def test_round14_sync_and_setup_rows_bind_observed_outcomes(tmp_path, monkeypatch):
    from test_w42_round14 import setup_command, sync_command

    text = _read("docs/cli/exit-codes.md")
    for family, label, observed in (
        (
            "report",
            "GitHub query failure",
            sync_command(tmp_path, monkeypatch, ["auth"])[0].exit_code,
        ),
        (
            "setup",
            "Setup check or installation findings",
            setup_command(tmp_path, monkeypatch).exit_code,
        ),
    ):
        row = next(
            line for line in text.splitlines() if line.startswith(f"| `{family}` |")
        )
        cells = [cell.strip() for cell in row.strip("|").split("|")][1:]
        assert label in cells[observed].split("; "), (family, observed, row)


def test_round17_report_row_binds_query_failure(tmp_path, monkeypatch):
    from test_w42_round17 import report_command

    result, content = report_command(tmp_path, monkeypatch)
    assert result.exit_code == 2, result.output
    assert "# kew spend report" in content
    row = next(
        line
        for line in _read("docs/cli/exit-codes.md").splitlines()
        if line.startswith("| `report` |")
    )
    cells = [cell.strip() for cell in row.strip("|").split("|")][1:]
    assert "GitHub query failure after partial report output" in cells[
        result.exit_code
    ].split("; ")
    report_page = _read("docs/cli/report.md")
    assert "returns exit code 2" in report_page
    assert "Report outcome sync incomplete: query failures N." in report_page
    assert "still renders (exit code 0)" not in report_page


def _public_exit_claims(text):
    return re.findall(
        r"\bexit(?:s|ed)?(?:[- ]code)?[\s:=`*-]*(\d+)\b"
        r"|^\| `(\d+)` \|",
        text,
        re.I | re.M,
    )


_RAW_PROCESS_EXIT_CLAIMS = {
    "sandbox-entrypoint-contract.md": (
        "Entrypoint exit `64` means the prompt is missing; entrypoint exit `78`",
        "Failure prints a static diagnostic and exits `78`",
        "including agent exits `64` and `78`",
        "acknowledgement without an agent event and exit `126` or `127`",
        "including exits `126` and `127`",
        "kernel's kill exits 137",
        'Resolve `PROMPT_FILE="${KEW_PROMPT_FILE:-/work/.kew-prompt.txt}"`; exit `64`',
    ),
    "cli/exit-codes.md": (
        "Acknowledgement without an agent event, exit `126` or `127`",
    ),
    "cli/review.md": ("git exit 255",),
    "design/2026-07-23-adversarial-review-stage.md": ("Reviewer crash / exit 143",),
}


@pytest.mark.policy
def test_round14_current_docs_never_advertise_public_exit_above_two():
    root = Path(__file__).resolve().parents[2]
    violations = []
    for page in (root / "docs").rglob("*.md"):
        relative = page.relative_to(root / "docs")
        text = page.read_text()
        # Exact internal process descriptions are not public command outcomes.
        for raw_claim in _RAW_PROCESS_EXIT_CLAIMS.get(relative.as_posix(), ()):
            text = text.replace(raw_claim, "internal process status")
        for groups in _public_exit_claims(text):
            code = next(value for value in groups if value)
            if int(code) > 2:
                violations.append(f"{relative}: exit {code}")
    assert not violations, "\n".join(violations)
    _assert_exit_claim_bindings(root / "docs")


@pytest.mark.parametrize(
    "claim",
    [
        "Exit 3 only when a model has no row",
        "not exit-3 drift",
        "exit code `4`",
        "public exit 127",
        "| `9` | Findings |",
    ],
)
def test_round14_public_exit_docs_guard_rejects_stale_claims(claim):
    assert any(
        int(next(value for value in groups if value)) > 2
        for groups in _public_exit_claims(claim)
    )


def _round18_exit_cell(family, code):
    row = next(
        line
        for line in _read("docs/cli/exit-codes.md").splitlines()
        if line.startswith(f"| `{family}` |")
    )
    return [cell.strip() for cell in row.strip("|").split("|")][code + 1].split("; ")


@pytest.mark.parametrize("mode", ["containment", "preflight", "manifest", "export"])
def test_round18_epic_plan_exit_table(tmp_path, monkeypatch, mode):
    from test_w42_round18 import plan_command

    from kew.graph.plan_run import run_epic_plan

    code, _, _ = plan_command(tmp_path, monkeypatch, mode)
    label = (
        "Planning pre-launch refusal"
        if mode in {"containment", "preflight"}
        else "Planning execution or validation failure"
    )
    assert label in _round18_exit_cell("epic", code)
    assert "2 could not run/query failure" in run_epic_plan.__doc__
    page = _read("docs/cli/epic.md")
    row = next(
        line
        for line in page.splitlines()
        if line.startswith(f"| `{code}` |")
        and ("pre-launch" in line or "manifest validation" in line)
    )
    assert {
        "containment": "containment refusal",
        "preflight": "runner preflight refusal",
        "manifest": "manifest validation failure",
        "export": "export failure after execution",
    }[mode] in row


def test_round18_epic_post_creation_exit_table(tmp_path, monkeypatch):
    from test_w42_round18 import post_creation_command

    code = post_creation_command(tmp_path, monkeypatch)
    assert "Post-creation GitHub query failure" in _round18_exit_cell("epic", code)
    row = next(
        line
        for line in _read("docs/cli/epic.md").splitlines()
        if line.startswith(f"| `{code}` |") and "post-creation" in line
    )
    assert "post-creation GitHub query failure" in row


def test_round18_report_repository_exit_table(tmp_path, monkeypatch):
    from test_w42_round18 import REPO_MESSAGE, _reply, unresolved_report

    result, *_ = unresolved_report(
        tmp_path, monkeypatch, mode="models", reply=_reply(code=1)
    )
    assert "Repository unavailable" in _round18_exit_cell("report", result.exit_code)
    assert REPO_MESSAGE in _read("docs/cli/report.md")


def test_round18_report_staleness_exit_table(tmp_path, monkeypatch):
    from test_w42_round18 import WARNING, report_command

    result, content = report_command(tmp_path, monkeypatch, mode="file")
    assert content.startswith(f"# {WARNING}\n\n")
    assert "GitHub query failure after partial report output" in _round18_exit_cell(
        "report", result.exit_code
    )
    page = _read("docs/cli/report.md")
    assert WARNING.replace(": 1 GitHub", ": N GitHub") in page
    assert '"outcome_sync": {"query_failures": N, "stale": true}' in page


# Every numeric exit assertion is inventoried, including wrapped prose and tables.
# Public bindings name a scheme outcome, never a second copy of its numeric code.
# Internal process examples carry explicit explanations in the same inventory.
_EXIT_ASSERTION = re.compile(
    r"\b(?:exit(?:s|ed|ing)?(?:[- ](?:codes?|status|class))?"
    r"(?:\s+(?:with|is|of))?(?:\s+(?:code|status|class))?"
    r"|returns?(?:\s+(?:exit(?:\s+(?:code|class))?|code|status|the repair code))?)"
    r"(?:\s+cleanly\s*\()?[\s:=`*]*(\d+)\b",
    re.I,
)
_EXIT_LITERAL = re.compile(r"[`*]+([012])[`*]+|/\s*([012])\b")
_EXIT_TABLE = re.compile(r"^\|\s*[`*]+(\d+)[`*]+\s*\|")
_EXIT_LIST = re.compile(r"^[-*] [`*]*(\d+)[`*]*\s*[—–:=]")
_EXIT_BINDINGS = REPO_ROOT / "kew/tests/fixtures/exit_claim_bindings.json"


def _exit_doc_blocks(text):
    """Keep table rows and list items separate; join wrapped prose sentences."""
    block = []
    line_number = 1
    for number, line in enumerate(text.splitlines(), 1):
        boundary = not line.strip() or re.match(r"\s*(?:\||[-*] |\d+\. |#|```)", line)
        if boundary and block:
            yield line_number, " ".join(" ".join(block).split())
            block = []
        if line.strip():
            if not block:
                line_number = number
            block.append(line.strip())
        if line.lstrip().startswith("|"):
            yield line_number, " ".join(" ".join(block).split())
            block = []
    if block:
        yield line_number, " ".join(" ".join(block).split())


def _exit_assertion_codes(block):
    matches = list(_EXIT_ASSERTION.finditer(block))
    table = _EXIT_TABLE.match(block) or _EXIT_LIST.match(block)
    if table:
        matches.append(table)
    if not matches and re.search(r"\bexit(?:s|ing|[- ]codes?| status)?\b", block, re.I):
        matches = list(_EXIT_LITERAL.finditer(block))
    if not matches and block.startswith("|") and "kew " in block:
        matches = list(re.finditer(r"\|\s*[`*]*(\d+)[`*]*\s*(?=\|)", block))
    if not matches:
        return [], block
    # Explicit literals include secondary codes in a compact 0/1/2 contract.
    first_code = min(match.start() for match in matches)
    matches.extend(_EXIT_LITERAL.finditer(block, first_code))
    spans = {}
    for match in matches:
        group = next(i for i, value in enumerate(match.groups(), 1) if value)
        spans[match.span(group)] = int(match.group(group))
    codes = []
    normalized = block
    for (start, end), code in sorted(spans.items(), reverse=True):
        codes.insert(0, code)
        normalized = normalized[:start] + "<code>" + normalized[end:]
    return codes, normalized


def _scheme_outcome_codes(text):
    outcomes = {}
    for family, cells in re.findall(r"^\| `([a-z-]+)` \| (.+) \|$", text, re.M):
        for code, cell in enumerate(cells.split(" | ")):
            for outcome in cell.split("; "):
                if outcome != "—":
                    outcomes[f"{family}::{outcome}"] = code
    return outcomes


def _assert_exit_claim_bindings(docs):
    bindings = json.loads(_EXIT_BINDINGS.read_text())
    known = {(entry["page"], entry["claim"]): entry for entry in bindings}
    assert len(known) == len(bindings), "Duplicate exit claim binding"
    scheme = _scheme_outcome_codes((docs / "cli/exit-codes.md").read_text())
    # A binding for a page that no longer exists is stale inventory.
    violations = sorted(
        f"{page}: binding names a missing page"
        for page in {entry["page"] for entry in bindings}
        if not (docs / page).is_file()
    )
    for page in sorted(docs.rglob("*.md")):
        relative = page.relative_to(docs).as_posix()
        # This is the authority. Its command bindings have boundary tests above.
        if relative == "cli/exit-codes.md":
            continue
        for line, block in _exit_doc_blocks(page.read_text()):
            codes, claim = _exit_assertion_codes(block)
            if not codes:
                continue
            entry = known.get((relative, claim))
            location = f"{relative}:{line}"
            if entry is None:
                violations.append(f"{location}: unbound exit claim: {block}")
                continue
            expected = [
                scheme[binding]
                if isinstance(binding, str)
                else binding["non_public_value"]
                for binding in entry["bindings"]
            ]
            if codes != expected:
                violations.append(
                    f"{location}: claimed {codes}, scheme requires {expected}: {block}"
                )
    assert not violations, "\n".join(violations)


@pytest.mark.policy
def test_round19_all_executable_exit_claims_bind_to_scheme():
    _assert_exit_claim_bindings(REPO_ROOT / "docs")


def test_driver_credential_docs_keep_authority_in_parent():
    for path in ("docs/security.md", "docs/cli/sandbox.md", "docs/cli/dispatch.md"):
        doc = _read(path)
        assert "The driver mints each detached run key before spawning" in doc
        assert "Children never hold management authority." in doc
        assert "KEW_ISSUED_RUN_KEY_HASH" in doc
        assert "expiry is the backstop" in doc
        assert "popen_kew_dispatch" not in doc


def test_round7_docs_bind_route_digest_and_no_fallback():
    for path in ("docs/security.md", "docs/runners/openrouter.md"):
        doc = _read(path)
        for claim in (
            "--credential-route <digest>",
            "resolved runner, model, transport, sidecar flag, and bounded-key decision",
            "credential_route_digest",
            "credential route changed",
            "exit 2, and reservation release before launch",
            "There is no fallback to the operator's `OPENROUTER_API_KEY`.",
            "from `--credential-route` or `--run-id`",
        ):
            assert claim in doc


def test_round8_docs_bind_required_credential_names():
    for path in ("docs/security.md", "docs/runners/openrouter.md"):
        doc = _read(path)
        assert (
            "An excluded handoff keeps only the resolved route's required "
            "credential names; "
            "the names are part of the route digest."
        ) in doc
        assert "Credential values are never part of the digest." in doc


def test_round10_issued_key_docs_bind_launch_evidence_and_publication_hold() -> None:
    for path in ("docs/runners/openrouter.md", "docs/security.md"):
        doc = _read(path)
        for claim in (
            "issued:openrouter:<hash>",
            "sandbox_resolved",
            "same run id and attempt",
            "issuer `openrouter` and the matching `key_hash`",
            "issued_key_evidence_missing",
            "revoke-before-publish",
            "credential_issuer.revoked=true",
            "issued_key_unrevoked",
            "issuer's reason",
            "A later retry releases publication after successful revocation "
            "evidence arrives.",
            "The publication gate reads evidence; it never calls the issuer.",
            "Raw ambient `env:` credentials publish only a head the host scanned "
            "clean immediately before the push.",
        ):
            assert claim in doc, (path, claim)


def test_round11_docs_bind_revocation_reconciliation_and_operator_recovery() -> None:
    for path in ("docs/runners/openrouter.md", "docs/security.md"):
        doc = _read(path)
        for claim in (
            "durable `revocation_pending` marker",
            "shared reconciler handles foreground keys and driver-issued keys",
            "Driver ticks, driver exit, and delegation watcher passes",
            "Foreground credential finalization and publication retries",
            "every producer attempt and ancestor run",
            "Retry delays start at 30 seconds, double, "
            "and stop increasing at 300 seconds",
            "retry counter saturates at 32; retries continue",
            "HTTP 404 or an explicit key-absence response records `revoked=true`",
            "reason `already absent`",
            "HTTP 401 or 403 records `invalid provisioning credential`",
            "one operator warning per key",
            "Replace `OPENROUTER_PROVISIONING_KEY` in the host process environment",
            "Key expiry alone never releases publication",
        ):
            assert claim in doc, (path, claim)


def test_round12_docs_bind_issuance_obligation_and_live_run_protection() -> None:
    for path in ("docs/runners/openrouter.md", "docs/security.md"):
        doc = _read(path)
        for claim in (
            "The host-authenticated issuance record itself is the durable "
            "revocation obligation.",
            "Recorded issuance survives a crash before the first DELETE marker.",
            "Recovery requires a terminal run row or explicit terminal evidence "
            "for an absent row.",
            "Recovery never revokes a pending run, even after key expiry.",
            "Each DELETE first persists a durable `revocation_pending` marker",
            "Predecessor publication jobs use the same reconciliation path.",
            "Key expiry alone never releases publication.",
        ):
            assert claim in doc, (path, claim)


def test_round13_docs_bind_prelaunch_protection_and_cleanup() -> None:
    for path in ("docs/runners/openrouter.md", "docs/security.md"):
        doc = _read(path)
        for claim in (
            "Absence of a run row is never treated as terminal by itself.",
            "The pre-launch window starts when the parent prepares a key",
            "Before issuance evidence, `mint_run_credential` commits a durable",
            "run id, key hash, issuing process pid, issuance time, and `expires_at`",
            "The pending row committed by `save` or `try_admit_run` "
            "supersedes the marker.",
            "A first terminal row also supersedes the marker",
            "Driver cleanup closes prepared-but-unspawned keys",
            "Cleanup writes a pre-launch tombstone before DELETE",
            "recovery requires a reaping tombstone, a pre-launch tombstone, "
            "or key expiry.",
            "A hard driver death before Popen leaves the marker protected "
            "until expiry.",
            "one operator warning lists the waiting run, key hash, issuing pid, "
            "and expiry.",
            "reconciliation never probes process liveness.",
        ):
            assert claim in doc, (path, claim)


def test_round14_docs_bind_prepost_intent_and_child_filtering() -> None:
    for path in ("docs/runners/openrouter.md", "docs/security.md"):
        doc = _read(path)
        for claim in (
            "Before POST, `mint_run_credential` commits an issuance INTENT",
            "New deterministic names use `kew-run:<ledger_id>:<run_id>`.",
            "Issuance without a bound ledger identity is refused "
            "with `issuer refused`.",
            "Projects may share one management account because each ledger "
            "has its own namespace.",
            "Sweeps and intent recovery act only within "
            "the current ledger’s namespace.",
            "Foreign, unqualified, and malformed names are skipped "
            "before ownership checks.",
            "deterministic key name, requested limit, expiry, "
            "issuing pid, and timestamp",
            "writes the pre-launch marker, records issuance evidence, "
            "then settles the intent",
            "The issuer reply's `data.name` must exactly equal the qualified name "
            "persisted in the issuance intent.",
            "Missing, unqualified, differently qualified, and foreign-ledger names "
            "raise `MalformedIssuerReply` before any credential reaches a caller.",
            "Malformed replies with validated hashes enter durable revocation "
            "before the error propagates.",
            "Ownership requires the listed name to equal the persisted qualified name; "
            "matching hashes alone do not establish ownership.",
            "Recovery waits until the requested expiry",
            "includes disabled keys, and matches the exact key name",
            "Matching keys require DELETE with confirmed deletion or absence",
            "negative lookup strictly after the requested expiry",
            "Unsupported or failed lookup leaves the intent open",
            "warning naming the run",
            "Generic children always lose both OpenRouter credentials",
            "Host agent launches also remove ambient OpenRouter credentials",
            "Management authority never enters a child environment",
        ):
            assert claim in doc, (path, claim)


def test_dispatch_reservation_docs_require_parent_pinning():
    text = _read("docs/security.md")
    assert "exact reservation ID acquired by their parent" in text
    assert "refusal releases nothing and publishes nothing" in text
    assert "prefers a pinned child over an unpinned child" in text


@pytest.mark.parametrize("page", ["docs/cli/epic.md", "docs/security.md"])
def test_round19_docs_bind_retained_claim_and_manual_recovery(page):
    text = " ".join(_read(page).split())
    for rule in (
        "a refusal retains the claim and never resets Linear automatically",
        "`issueUpdate` has no version precondition",
        "`linear_claim_retained` records the refusal reason, reservation identity, "
        "claim snapshot's `updatedAt`, and recovery instructions",
        "The `on_refuse` callback finishes before the terminal refusal row, "
        "event, or reservation release",
    ):
        assert rule in text
    assert "`linear_claim_rollback`" not in text
    assert "`linear_claim_rollback_skipped`" not in text


@pytest.mark.parametrize(
    "page", ["docs/cli/epic.md", "docs/cli/delegate.md", "docs/security.md"]
)
def test_round20_docs_bind_exact_retained_claim_recovery(page):
    from kew.claim_retention import LINEAR_CLAIM_RECOVERY

    text = " ".join(_read(page).split())
    assert LINEAR_CLAIM_RECOVERY in text
    assert "An abandon verb is being added separately." not in text
    assert "No kew command releases a retained claim" in text
    if page == "docs/security.md":
        # #2005: the security page names where the re-claim comes from rather
        # than a verb "being added".
        assert "abandon verb is being added" not in text
        assert (
            "No kew command releases a retained claim: the replacement dispatch "
            "comes from the epic, whose tick re-dispatches the node and "
            "re-claims the item"
        ) in text
        # ``kew dispatch`` takes only a GitHub issue number, so it cannot be
        # the verb that re-claims a Linear item.
        assert "`kew dispatch <item>`" not in text
    assert "`kew epic status` shows a `retained claim` line" in text
    assert "latest recorded reason and the current recovery instructions" in text
    # PR #1952 documents `kew epic abandon` for preserved continuations on
    # docs/cli/epic.md, so the page-wide check is scoped there to the
    # retained-claim recovery passage, which still must not name the verb.
    start = text.index(LINEAR_CLAIM_RECOVERY)
    end = text.find("Historical events use current", start)
    passage = text[start : end if end >= 0 else len(text)]
    assert "`kew epic abandon`" not in passage
    if page != "docs/cli/epic.md":
        assert "`kew epic abandon`" not in text
    assert "Otherwise unclaim it in Linear or run" not in text


def test_prompt_outcome_window_starts_at_admission_without_a_started_receipt():
    """The outcome window opens at launch only when the launch receipt reached
    ``started``; an ``unknown`` receipt or a legacy row starts at admission."""
    text = " ".join(_read("docs/security.md").split())
    assert (
        "A run's outcome window starts when the run launched if its launch "
        "receipt reached `started`, and when it was admitted otherwise"
    ) in text
    assert "starts when the run launched, not when it was admitted" not in text


def test_host_liveness_requires_kernel_tokens() -> None:
    text = _read("docs/cli/dispatch.md")
    for claim in (
        "Host execution requires non-null `owner_start_token` "
        "and `attempt_start_token`",
        "owner-identity-unavailable",
        "attempt-identity-unavailable",
        "platform and its inability",
        "libproc",
        "/proc/<pid>/stat",
        "three times",
        "fallback never authorizes host execution or a kill target",
        "exit code 2",
        "`not-run`",
    ):
        assert claim in text


def test_credential_cleanup_uses_owner_and_container_recovery():
    document = _read("docs/cli/dispatch.md")
    for claim in (
        "Credential reconciliation preserves every pending run.",
        "Owner and durable-container recovery must confirm death",
        "Unknown ownership or container state keeps the credential unrevoked",
    ):
        assert claim in document


def test_loop_agent_containment_is_stated_where_operators_read():
    """#1977: loops are not contained; docs and help say so and name the flag."""
    from typer.testing import CliRunner

    from kew.cli import app

    flag = "dangerously_skip_containment = true"
    for path in ("README.md", "docs/getting-started.md"):
        text = " ".join(_read(path).split())
        assert "kew has no contained loop launch" in text, path
        assert flag in text, path
        assert "Review, resolve, loops, ci-fix" not in text, path
    for path in ("docs/cli/loop.md", "docs/security.md"):
        text = " ".join(_read(path).split())
        assert "`kew dispatch --loop` and `kew loop resume` refuse" in text, path
        assert "refuse with exit code 2" in text, path
        assert "before any checkout, agent work, or spend" in text, path
        assert 'posture = "uncontained"' in text, path
    assert "do not resolve an agent" not in _read("docs/security.md")
    result = CliRunner().invoke(app, ["dispatch", "--help"], env={"COLUMNS": "200"})
    help_text = " ".join(result.output.split())
    assert "The loop agent runs on the host" in help_text
    assert "dangerously_skip_containment" in help_text


# --- 2026-09-22 re-audit docs truth (#1983) ---------------------------------


def _exit_family_cells(family):
    row = next(
        line
        for line in _read("docs/cli/exit-codes.md").splitlines()
        if line.startswith(f"| `{family}` |")
    )
    return [cell.strip().split("; ") for cell in row.strip("|").split("|")][1:]


def test_f15_epic_run_exit_rows_bind_to_execution(tmp_path, monkeypatch):
    from test_epic_driver_cli import _run_all, _run_single, _tick_result

    from kew import ledger_identity
    from kew.graph.driver.result import StuckNode, TerminalSignal

    # Keep ledger discovery and registries inside this test's project.
    find_git_entry = ledger_identity._find_git_entry

    def fixture_git_entry(start):
        entry = find_git_entry(start)
        return entry if entry is not None and entry.is_relative_to(tmp_path) else None

    monkeypatch.setattr(ledger_identity, "_find_git_entry", fixture_git_entry)
    monkeypatch.setattr("kew.cli.project_root", lambda: tmp_path)

    cells = _exit_family_cells("epic")
    waiting = (
        StuckNode(node_id="2", reason="in_review — awaiting merge", blast_radius=()),
    )
    stalled = _tick_result(remaining=1, waiting=waiting, signal=TerminalSignal.STALLED)
    locked = _tick_result(remaining=1, locked_out=True)
    for run, label in (
        (
            lambda: _run_single(tmp_path, stalled),
            "Stalled with operator repair required",
        ),
        (
            lambda: _run_all(tmp_path, {"a": stalled}),
            "Stalled with operator repair required",
        ),
        (lambda: _run_single(tmp_path, locked), "Epic driven elsewhere"),
        (lambda: _run_all(tmp_path, {"a": locked}), "Epic driven elsewhere"),
    ):
        result = run()
        assert result.exit_code in (0, 1), result.output
        assert label in cells[result.exit_code], (label, result.exit_code)
    assert "awaiting-merges" not in _read("docs/cli/epic.md")


def test_f15_epic_retry_without_driver_binds_to_repair_code(tmp_path, monkeypatch):
    from unittest.mock import MagicMock, patch

    from typer.testing import CliRunner

    from kew.cli import app
    from kew.config import DispatchConfig

    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    cfg = DispatchConfig()
    cfg.project.repo = "o/r"
    cfg.logging.dir = str(tmp_path / "logs")
    store = MagicMock()
    store.__enter__.return_value = MagicMock()
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._epic_repo", return_value="o/r"),
        patch("kew.cli._open_run_store", return_value=store),
        patch("kew.cli.service_verbs.epic_retry"),
    ):
        result = CliRunner().invoke(app, ["epic", "retry", "epic:x", "42"])
    message = "no live driver — relaunch required: kew epic run epic:x"
    assert message in result.output
    cells = _exit_family_cells("epic")
    assert "Retry recorded with no live driver" in cells[result.exit_code]
    page = " ".join(_read("docs/cli/epic.md").split())
    assert (
        "it exits **1** with `no live driver — relaunch required: "
        "kew epic run <name>`" in page
    )


def test_f15_export_and_report_rows_bind_to_execution(tmp_path, monkeypatch):
    from unittest.mock import patch

    from typer.testing import CliRunner

    from kew.cli import app
    from kew.notifier import ReportDeliveryError
    from kew.report import ReportResult

    export = _exit_family_cells("export")
    assert export[1] == ["—"]
    result = CliRunner().invoke(app, ["export", "--format", "xml"])
    assert "invalid format, date, or option combination" in export[result.exit_code]

    report = _exit_family_cells("report")
    assert report[1] == ["—"]
    (tmp_path / "kew.toml").write_text('[project]\nrepo = "o/r"\n')
    monkeypatch.chdir(tmp_path)
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._detect_repo"),
        patch("kew.cli.build_report_result", return_value=ReportResult("# spend")),
        patch("kew.cli.deliver_report", side_effect=ReportDeliveryError("POST")),
    ):
        result = CliRunner().invoke(app, ["report", "--notify"])
    assert "Webhook delivery failure" in report[result.exit_code]


@pytest.mark.policy
def test_f16_loop_docs_name_only_real_commands():
    from typer.testing import CliRunner

    from kew.cli import app

    help_text = CliRunner().invoke(app, ["loop", "--help"]).output
    assert not re.search(r"^\s*│?\s*run\b", help_text, re.M)
    for page in sorted((REPO_ROOT / "docs").rglob("*.md")):
        relative = page.relative_to(REPO_ROOT / "docs").as_posix()
        if relative.startswith(("audit/", "design/")):
            continue
        assert "kew loop run" not in page.read_text(), relative
    loop = " ".join(_read("docs/cli/loop.md").split())
    assert "start a fresh loop with `kew dispatch <issue> --loop`" in loop


_ROUTING_PAGES = (
    "README.md",
    "docs/configuration.md",
    "docs/runners/index.md",
    "docs/runners/codex.md",
    "docs/runners/opencode.md",
    "docs/runners/openrouter.md",
    "docs/runners/hermes.md",
)


def test_f17_every_routing_snippet_loads_and_opencode_routes_reach_openrouter():
    from kew.config import RoutingConfig

    catalog = json.loads(
        (REPO_ROOT / "kew/src/kew/_docker/docker/opencode.json").read_text()
    )
    catalog_ids = {
        f"openrouter/{model}" for model in catalog["provider"]["openrouter"]["models"]
    }
    fence = re.compile(r"```toml\n(.*?)```", re.S)
    loaded = 0
    for path in _ROUTING_PAGES:
        text = _read(path)
        for body in fence.findall(text):
            routing = tomllib.loads(body).get("routing")
            if routing is None:
                continue
            RoutingConfig.model_validate(routing)
            loaded += 1
        # Contained OpenCode reaches only OpenRouter, commented examples included.
        for model in re.findall(
            r'runner\s*=\s*"opencode",\s*model\s*=\s*"([^"]+)"', text
        ):
            assert model.startswith("openrouter/"), (path, model)
    assert loaded >= 9
    for path in ("README.md", "docs/runners/index.md", "docs/configuration.md"):
        flagship = re.findall(
            r'simple\s*=\s*\{\s*runner\s*=\s*"opencode",\s*model\s*=\s*"([^"]+)"',
            _read(path),
        )
        assert flagship and set(flagship) <= catalog_ids, (path, flagship)


def test_f18_cleanup_dry_run_doc_matches_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import UTC, datetime, timedelta

    from kew import cli
    from kew.run_store import RunRecord, SqliteRunStore

    # The preview reuses the sweep's own staleness rule, not a copy of it: on
    # the same ledger it names exactly the rows the real sweep then takes, and
    # changes none of them. The stale row's owner is a pid and group no kernel
    # hands out, so its host death is proven against the real process table.
    db = tmp_path / "dispatch.db"
    now = datetime.now(UTC)
    with SqliteRunStore(str(db)) as store:
        for run_id, issue, age in (("run-stale", 77, 48), ("run-fresh", 78, 1)):
            store.save(
                RunRecord(
                    run_id=run_id,
                    issue_num=issue,
                    branch=f"agent/issue-{issue}",
                    status="pending",
                    exit_code=0,
                    started_at=now - timedelta(hours=age),
                    pgid=2**31 - 1,
                    owner_pid=2**31 - 1,
                    owner_start_time="Tue Sep 15 10:00:00 2026",
                    attempt_pgid=2**31 - 2,
                )
            )
        before = store.get("run-stale")
    assert cli._cleanup_pending_runs(db, dry_run=True) == (1, {78})
    preview = capsys.readouterr()
    assert "Would clean up 1 stale pending record(s)" in preview.out + preview.err
    with SqliteRunStore(str(db)) as store:
        assert store.get("run-stale") == before
    assert cli._cleanup_pending_runs(db, dry_run=False) == (1, {78})
    with SqliteRunStore(str(db)) as store:
        swept = store.get("run-stale")
        assert swept is not None and swept.status == "failure"
    page = " ".join(_read("docs/cli/cleanup.md").split())
    # The dry run still opens the store (#1991): claim only what it leaves alone.
    assert "`--dry-run` changes no run records and no labels." in page
    assert "`--dry-run` changes nothing" not in page
    assert "It still opens the run store" in page
    assert "It does not preview stop-label resets that `kew stop` left queued." in page
    # The preview does not model the reset outbox, so no sentence may promise
    # that it always matches the real run.
    assert "exit code matches the real run" not in page
    assert "reports what `kew cleanup` would do" not in page


_PROVISIONING_PAGES = (
    "docs/cli/sandbox.md",
    "docs/authentication.md",
    "docs/runners/openrouter.md",
    "docs/runners/hermes.md",
    "docs/security.md",
)


def test_f19_provisioning_key_required_on_every_direct_route():
    from kew.credential_handoff import direct_openrouter_route

    # The route decision takes no containment input: host and container agree.
    for model in ("openrouter/google/gemini-3.8-flash", "openrouter/qwen/x"):
        assert direct_openrouter_route(model, ("OPENROUTER_API_KEY",), direct=True)
    anchor = "#bounded-openrouter-credentials"
    sandbox = " ".join(_read("docs/cli/sandbox.md").split())
    assert (
        "Every direct OpenRouter route requires `OPENROUTER_PROVISIONING_KEY` "
        "in the host environment. This applies on the host and in a container."
        in sandbox
    )
    for path in _PROVISIONING_PAGES:
        text = " ".join(_read(path).split())
        if path != "docs/authentication.md":
            # authentication.md states the host rule in the same paragraph.
            assert "Contained direct OpenRouter runs require" not in text, path
        assert "Contained direct routes refuse" not in text, path
        assert "for bounded issuance" not in text, path
        assert "kew does not see or store it" not in text, path
    for path in ("docs/authentication.md", "docs/runners/openrouter.md"):
        assert anchor in _read(path), path
    assert "openrouter.md#setup" in _read("docs/runners/hermes.md")


def test_f20_security_policy_matches_publication_and_containment_defaults():
    from kew.config import PublicationConfig, SandboxConfig

    assert PublicationConfig().publish == "canonical"
    assert SandboxConfig().enabled == "auto"
    text = " ".join(_read("SECURITY.md").split())
    assert "legacy push path" not in text
    assert "publication broker is dormant" not in text
    assert "When `[sandbox] enabled = true`, each dispatch" not in text
    assert 'the default `[publication] publish = "canonical"`' in text
    assert (
        'Containment is active under the default `[sandbox] enabled = "auto"`' in text
    )


def test_f21_onboarding_surfaces_mirror_getting_started():
    readme = " ".join(_read("kew/README.md").split())
    for token in (
        "kew sandbox build",
        "kew doctor",
        "kew-allowlist",
        "ANTHROPIC_API_KEY",
        "standalone clone",
    ):
        assert token in readme, token
    assert "worktree" not in readme
    # create-kew is a launcher: the follow-up steps are kew init's, not its own.
    launcher = _read("create-kew/index.js")
    assert '["init", ...args]' in launcher
    action = _read("create-kew/templates/kew-action.yml")
    block = action[action.index("permissions:") : action.index("\non:")]
    for grant in ("contents: write", "issues: write", "pull-requests: write"):
        assert grant in block, grant


def test_f24_doctor_hint_leads_with_api_key_and_carries_caveat():
    from kew.runners.claude_code import ClaudeCodeRunner
    from kew.sandbox import credential_missing_message

    hint = credential_missing_message(
        "claude-code", ClaudeCodeRunner().required_credentials
    )
    assert hint.index("console.anthropic.com") < hint.index("claude setup-token")
    assert "do not use subscription auth for autonomous dispatch" in hint
    assert "Do not use Pro/Max subscription OAuth" in _read("docs/authentication.md")


def test_f11_codex_read_scope_is_stated():
    from kew.config import DispatchConfig
    from kew.runners.codex import CodexRunner

    command = CodexRunner().build_command(
        prompt="fix it", config=DispatchConfig(), env={}, model="gpt-5.6-sol"
    )
    assert command[command.index("--sandbox") + 1] == "workspace-write"
    for path in ("docs/security.md", "docs/threat-model.md"):
        text = " ".join(_read(path).split())
        assert (
            "`workspace-write` restricts writes and command network access, "
            "not reads." in text
        ), path
        assert "refuses output that contains a credential value kew holds" in (text), (
            path
        )
        assert "is caught only by its shape." in text, path
    security = _read("SECURITY.md").replace("\n  ", " ")
    assert "`workspace-write` restricts writes and network, not reads." in security
    assert "and refuses publication on a match." in security


def test_publication_output_scan_docs_match_the_scanner():
    from kew.publication_credentials import _MESSAGES
    from kew.publication_scan import KEY_SHAPES, MIN_VALUE_CHARS, OUTPUT_SCAN_EVENT

    text = " ".join(_read("docs/security.md").split())
    assert "### Publication output scan" in _read("docs/security.md")
    assert f"`{OUTPUT_SCAN_EVENT}`" in text
    assert f"values shorter than {MIN_VALUE_CHARS} characters are not scanned" in text
    for reason in (
        "credential_in_output",
        "credential_scan_missing",
        "credential_scan_unavailable",
        "credential_scan_oversize",
        "credential_scan_timeout",
    ):
        assert reason in _MESSAGES
        assert f"`{reason}`" in text
    assert set(KEY_SHAPES) == {
        "anthropic_key_shape",
        "openrouter_key_shape",
        "github_token_shape",
        "openai_key_shape",
    }
    for shape in (
        "`sk-ant-`",
        "`sk-or-v1-`",
        "`gh[pousr]_`",
        "`github_pat_`",
        "`sk-proj-`",
        "`sk-svcacct-`",
        "`sk-admin-`",
    ):
        assert shape in text
    assert "`$CODEX_HOME/auth.json`, else `~/.codex/auth.json`" in text
    from kew.publication_scan import output_scan_names

    for name in output_scan_names():
        assert f"`{name}`" in text, name
    assert "publication-output-scan" in _read("docs/threat-model.md")


def test_already_published_refusal_docs_match_the_guard():
    from kew import publication_canonical, worktree
    from kew.publication_credentials import _MESSAGES
    from kew.publication_scan import ALREADY_PUBLISHED_EVENT

    text = " ".join(_read("docs/security.md").split())
    for claim in (
        "On a path where the host pushes, a finding refuses with reason "
        "`credential_in_output`: nothing is pushed,",
        "On the republish of an agent-pushed review head, the agent pushed its "
        "commits itself before the host scan, so a finding there is already on "
        "the remote branch.",
        "For a finding in the commits the agent pushed, the host refuses with "
        "reason `credential_already_published`, marks the scan record "
        "`already_published`,",
        "the remote does not hold that commit, so a finding only there refuses "
        "with `credential_in_output`, like any host push.",
        "A recorded finding is final for its head.",
        f"records a `{ALREADY_PUBLISHED_EVENT}` audit event",
        "The operator must rotate that credential and remove the commit from the "
        "branch.",
        "On this path kew moves the branch tip in two ways, each with "
        "`--force-with-lease` against the agent head it verified: to the "
        "host-built republish commit, or, for a change-then-revert head, back to "
        "the reviewed head.",
        "It never rewrites history it has not verified, and it never removes a "
        "commit with a finding; removal is the operator's action.",
        "the host fetches that range whole, up to "
        f"{publication_canonical.AGENT_RANGE_MAX_COMMITS} commits deep",
        "A range it cannot fetch whole, or a head that does not descend from the "
        "reviewed head, refuses with `credential_scan_unavailable`.",
    ):
        assert claim in text, claim

    # The republish path's two tip moves, each leased to the verified head.
    # Read off the syntax tree: the lease value is compared as a parsed
    # f-string template, so a reformat of the call cannot move this.
    def leases(node: ast.AST) -> list[str | None]:
        return [
            source_ast.template(lease)
            for call in source_ast.calls(node)
            if (lease := source_ast.keyword(call, "force_with_lease")) is not None
        ]

    verified = "{branch}:{observed_sha}"
    republish = source_ast.definition(publication_canonical, "republish_agent_head")
    reset = source_ast.definition(publication_canonical, "_reset_to_reviewed_head")
    for tip_move in (republish, reset):
        assert leases(tip_move) == [verified], tip_move.name
    module = source_ast.module_tree(publication_canonical)
    assert leases(module).count(verified) == 2
    assert source_ast.calls(republish, "_complete_agent_range")
    # The claims name real behaviour: the message, and the republish marking.
    assert "nothing was pushed" in _MESSAGES["credential_in_output"]
    assert (
        "rotate that credential and remove the commit"
        in (_MESSAGES["credential_already_published"])
    )
    assert "nothing was pushed" not in _MESSAGES["credential_already_published"]
    marked = [
        call
        for call in source_ast.calls(republish)
        if isinstance(commit := source_ast.keyword(call, "commit_sha"), ast.Name)
        and commit.id == "observed_sha"
        and isinstance(
            flag := source_ast.keyword(call, "already_published"), ast.Constant
        )
        and flag.value is True
    ]
    assert marked, (
        "the republish no longer marks the agent's own head already published"
    )
    host_republish = source_ast.definition(worktree, "republish_agent_head")
    assert "already_published" not in source_ast.identifiers(host_republish)


def test_codex_loop_docs_match_admission():
    from types import SimpleNamespace

    from kew.config import DispatchConfig
    from kew.loops.delegate import loop_route_rejection, resolve_loop_agent_posture
    from kew.runners.codex import CodexRunner

    assert resolve_loop_agent_posture(DispatchConfig(), runner="codex") == "native"
    route = SimpleNamespace(runner=CodexRunner())
    assert loop_route_rejection(route, publish="canonical") is None
    assert loop_route_rejection(route, publish="checkout") is not None
    claim = "Codex contains itself, so its loop needs no `dangerously_skip_containment`"
    for path in ("docs/cli/dispatch.md", "docs/cli/loop.md"):
        text = " ".join(_read(path).split())
        assert claim in text, path
        assert "fails closed for Codex" not in text, path
    dispatch = " ".join(_read("docs/cli/dispatch.md").split())
    assert 'With `publish = "checkout"`, `--loop` refuses Codex' in dispatch


@pytest.mark.policy
def test_loop_refusals_name_the_real_loop_command():
    """#1985: a fresh loop starts with ``kew dispatch <issue> --loop``.

    ``kew loop`` has no ``run`` subcommand, so a refusal that names one sends
    the operator to a command that fails. The docs half is bound by
    ``test_f16_loop_docs_name_only_real_commands``; this binds the messages in
    ``src/`` and the option they name.
    """
    import click
    import typer

    from kew.cli import app

    dispatch = typer.main.get_command(app).commands["dispatch"]
    loop = [param for param in dispatch.params if "--loop" in param.opts]
    assert len(loop) == 1
    assert isinstance(loop[0], click.Option) and loop[0].is_flag
    # Every string literal in src/, parsed: a message rewrapped across
    # implicitly concatenated pieces is one value here, so it cannot hide.
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in sorted((REPO_ROOT / "kew/src").rglob("*.py"))
        if any(
            "kew loop run" in text
            for text in source_ast.texts(ast.parse(path.read_text(encoding="utf-8")))
        )
    ]
    assert offenders == []


def test_archived_trees_stay_guarded_outside_check_docs() -> None:
    """The deploy's plain mkdocs build and the docs workflow trigger both guard
    the launch cut without relying on scripts/check-docs.sh (#1988 round 4).
    """
    mkdocs = (REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    exclude = mkdocs.split("exclude_docs: |", 1)[1].split("\n\n", 1)[0]
    assert "/audit/**" in exclude.split()

    workflow = (REPO_ROOT / ".github/workflows/docs.yml").read_text(encoding="utf-8")
    # Built from fragments so the gate's own scan does not read this line.
    for prefix in ("'kew/docs/**'", "'evidence-" + "packs/**'", "'.kew/**'"):
        assert workflow.count(prefix) == 2, prefix


def test_docs_build_renders_explicit_ids_and_fails_on_a_broken_anchor() -> None:
    """#1909: `{#id}` headings rendered as text without attr_list (ids like
    `delegation-delegation`), and a link to a missing anchor built clean
    because mkdocs 1.6 reports it at INFO. attr_list renders the ids, and
    `validation.anchors: warn` makes scripts/check-docs.sh's `mkdocs build
    --strict` fail on a broken fragment.
    """
    import yaml

    config = yaml.safe_load((REPO_ROOT / "mkdocs.yml").read_text(encoding="utf-8"))
    extensions = [
        next(iter(ext)) if isinstance(ext, dict) else ext
        for ext in config["markdown_extensions"]
    ]
    explicit_ids = [
        str(page.relative_to(REPO_ROOT))
        for page in sorted((REPO_ROOT / "docs").rglob("*.md"))
        if re.search(r"^#+ .*\{#[\w-]+\}\s*$", page.read_text(encoding="utf-8"), re.M)
    ]
    assert explicit_ids, "no explicit heading ids left: attr_list pin is stale"
    assert "attr_list" in extensions, explicit_ids
    assert config["validation"]["anchors"] == "warn"
    gate = (REPO_ROOT / "scripts/check-docs.sh").read_text(encoding="utf-8")
    assert "mkdocs build --strict" in gate


@pytest.mark.policy
def test_public_docs_do_not_name_the_removed_exposure_refusal() -> None:
    """#1969 replaced the refusal with the pre-push scan; no page may claim it."""
    # The refusal's name and its prose form ("refuses ... `env:` credential
    # exposure") both claim the behaviour the scan replaced.
    removed = (
        "contained_credential_" + "exposure",
        "refuses contained `env:` credential " + "exposure",
    )
    pages = [REPO_ROOT / "README.md", REPO_ROOT / "SECURITY.md"]
    pages += sorted((REPO_ROOT / "docs").rglob("*.md"))
    offenders = [
        str(page.relative_to(REPO_ROOT))
        for page in pages
        if page.exists()
        and any(claim in page.read_text(encoding="utf-8") for claim in removed)
    ]
    assert offenders == []


# --- #2030: six-month audit docs-truth bindings -----------------------------


def _flat(rel: str) -> str:
    return " ".join(_read(rel).split())


def test_2030_threat_model_names_the_unhashed_audit_columns() -> None:
    """Non-goal 3 says which rows the entry hash covers item_id and dedup_key
    on, and that rows from before the format step still leave them out (#2029)."""
    import dataclasses

    from kew.audit_chain import ChainRow

    hashed = {f.name for f in dataclasses.fields(ChainRow)}
    threat = _flat("docs/threat-model.md")
    assert "any after-the-fact edit breaks the chain" not in threat
    assert "#2029" in threat
    assert {"item_id", "dedup_key", "chain_format"} <= hashed
    assert (
        "On every row written from schema v77 on it also covers the row's chain"
        " format and the `item_id` and `dedup_key` columns."
    ) in threat
    assert (
        "Rows written before schema v77 still leave the `item_id` and"
        " `dedup_key` columns out of the hash"
    ) in threat


# Every variable each prompt template interpolates, by where its value comes
# from. A new variable fails the test below until it is classified here, and an
# attacker-reachable unfenced one must also be named on the security page.
_FENCED = "fenced"
_UNFENCED = "attacker-reachable, unfenced"
_TRUSTED = "trusted (kew-computed, numeric or operator config)"
_PROMPT_VARIABLES: dict[str, dict[str, str]] = {
    "dispatch.md.j2": {
        "issue_body_fenced": _FENCED,
        # The body's own acceptance bullets, restated verbatim under the same
        # per-render sentinel (#2290).
        "acceptance_criteria_fenced": _FENCED,
        "plan_sha": _TRUSTED,  # host-computed digest of the recorded plan
        # Every other attacker-reachable slot, fenced as data too (#3348).
        "issue_title_fenced": _FENCED,
        "repo_map_fenced": _FENCED,
        "recently_merged_context_fenced": _FENCED,
        "branch_name": _TRUSTED,  # built from branches.prefix and the issue id
        "base_branch": _TRUSTED,  # branches.base, or the host repo default branch
        "issue_num": _TRUSTED,
        "complexity": _TRUSTED,
        "complexity_guidance": _TRUSTED,
        "commands.ci": _TRUSTED,
        "commands.format": _TRUSTED,
        "commands.verify": _TRUSTED,
        "commands.targeted": _TRUSTED,
        "commands.policy": _TRUSTED,
        "commands.static": _TRUSTED,
        "memory_cap": _TRUSTED,
        "merge_base_branch": _TRUSTED,
        "commands.policy_mark": _TRUSTED,
        "commands.co_author": _TRUSTED,
        "commands.commit_prefix": _TRUSTED,
        "labels.agent_pr": _TRUSTED,
        "prompt_append": _TRUSTED,
        # An approved prompt profile (#3022): the body is proposed and
        # activated only by an ``[approval].approvers`` actor, read from the
        # host store (never a checkout), with an audit event per decision. It
        # renders unfenced as operator instructions, below which the safety
        # rules state their precedence.
        "profile_addendum.role": _TRUSTED,
        "profile_addendum.version": _TRUSTED,
        "profile_addendum.body": _TRUSTED,
        # The pre-publish self-check (#2454): host-authored prose, the recorded
        # plan's host-authored criterion IDS (never their statements, which stay
        # in the fenced block above), and the sha-pinned reviewer rubric quoted
        # as instructions — the same artifact, loaded the same base-ref-wins
        # way, that the review stage already trusts unfenced.
        "self_check_section": _TRUSTED,
    },
    # Included by ci-fix, resolve and review (#3023); the same addendum, so
    # classified as dispatch's is above.
    "_operator_addendum.md.j2": {
        "profile_addendum.role": _TRUSTED,
        "profile_addendum.version": _TRUSTED,
        "profile_addendum.body": _TRUSTED,
    },
    "ci-fix.md.j2": {
        "ci_log_fenced": _FENCED,
        "issue_title_fenced": _FENCED,
        "diff_summary_fenced": _FENCED,
        "branch_name": _UNFENCED,  # the PR's head ref
        "attempt": _TRUSTED,
        "max_attempts": _TRUSTED,
        "issue_number": _TRUSTED,
        "pr_number": _TRUSTED,
        "commands_ci": _TRUSTED,
        "commands_format": _TRUSTED,
    },
    "resolve.md.j2": {
        "conflicted_files_fenced": _FENCED,
        "branch_name": _UNFENCED,
        "base_branch": _UNFENCED,
        "issue_number": _TRUSTED,
        "pr_number": _TRUSTED,
        "commands_ci": _TRUSTED,
        "commands_format": _TRUSTED,
    },
    "review.md.j2": {
        "c.body": _FENCED,
        "c.author": _FENCED,
        "path": _FENCED,
        "sentinel": _TRUSTED,
        # #2200: test paths kew derived from the branch diff, and the package
        # directory each set is relative to. Both come from the checkout's own
        # file names, never from a comment body.
        "test_path": _TRUSTED,
        "group.run_dir": _TRUSTED,
        "untrusted_preamble": _TRUSTED,
        "branch_name": _UNFENCED,
        "base_branch": _UNFENCED,
        # The merge-conflict section's branch names; its paths are ``path``
        # (#2470, #3348).
        "conflict.branch": _FENCED,
        "conflict.base": _FENCED,
        "c.line": _TRUSTED,
        "repo": _TRUSTED,
        "issue_number": _TRUSTED,
        "pr_number": _TRUSTED,
        "commands_ci": _TRUSTED,
        "commands_format": _TRUSTED,
        "commands_test_targeted": _TRUSTED,
        "commands_test_policy_mark": _TRUSTED,
        "commands_test_policy_command": _TRUSTED,
        "commands_static": _TRUSTED,
        "memory_cap": _TRUSTED,
        # #2291: the recorded criterion ids the round judged failed. kew's own
        # ``AC1``..``ACn`` labels, generated by extraction and kept on the
        # ledger row; the parser drops any ``criterion`` a finding names that
        # the recorded plan does not state, so no reviewer-authored string
        # reaches this variable.
        "failing_criteria": _TRUSTED,
        # #2872: the node ids a declaration-only round asks for are read off
        # the host-recorded integrity report and refused unless they are
        # plain node-id characters; the reviewer's reasons are fenced.
        "declaration_ids": _TRUSTED,
        "node_id": _TRUSTED,
        "reason": _FENCED,
        # #3212: the base tests the pre-review held-out check found failing
        # undeclared, read off kew's own report plugin's verdict and refused
        # unless they are plain node-id characters; the PR body's Behavior
        # changes lines are fenced.
        "held_out_ids": _TRUSTED,
        "line": _FENCED,
        # #3493: the node ids a proposal-only round asks lines for are the
        # reviewer's declaration findings' ids, refused unless every one is
        # plain node-id characters (no space, no markdown link); the
        # reviewer's reasons are fenced as ``reason``.
        "proposal_ids": _TRUSTED,
    },
    "epic-plan.md.j2": {
        "epic_name": _TRUSTED,
        "plan_path": _TRUSTED,
        "manifest_relpath": _TRUSTED,
        "project_name": _TRUSTED,
        "criteria_min": _TRUSTED,
        "criteria_max": _TRUSTED,
    },
    # The triage station (#2293). Every attacker-reachable string in it is
    # fenced — the item's title and body, and the candidate titles the host's
    # duplicate search returned. The rest is host-computed: the body features
    # are regex measurements of the body, never its text, and ``labels`` /
    # ``unavailable`` are names (a label an operator applied, a reader that
    # could not run), not item prose.
    "triage.md.j2": {
        "title_block": _FENCED,
        "body_block": _FENCED,
        "candidate_block": _FENCED,
        "item_id": _TRUSTED,
        "labels": _TRUSTED,
        "unavailable": _TRUSTED,
        "features.length": _TRUSTED,
        "features.has_steps": _TRUSTED,
        "features.has_expected_actual": _TRUSTED,
        "features.has_version": _TRUSTED,
        "features.has_stack_trace": _TRUSTED,
    },
}


@pytest.mark.policy
def test_2030_every_prompt_variable_is_classified() -> None:
    templates = REPO_ROOT / "kew/src/kew/_templates"
    found = {
        path.name: set(re.findall(r"\{\{\s*([A-Za-z_][\w.]*)", path.read_text()))
        for path in templates.glob("*.j2")
    }
    assert {name: set(v) for name, v in _PROMPT_VARIABLES.items()} == found


def test_2030_security_page_lists_unfenced_prompt_fields() -> None:
    security = _flat("docs/security.md")
    assert "Every dispatch surface renders" not in security
    assert "Other attacker-reachable text is interpolated **unfenced**" in security
    for field in (
        "the PR's head branch name (review, resolve, ci-fix) and base branch name",
    ):
        assert field in security, field
    unfenced = security.split("interpolated **unfenced**", 1)[1].split(
        "An approved prompt profile", 1
    )[0]
    # Fenced as data since #3348, so no longer on the unfenced list.
    for field in ("issue title", "repo map", "recently merged", "diff_summary"):
        assert field not in unfenced, field


def test_2030_guard_scope_is_claude_code_only() -> None:
    security = _flat("docs/security.md")
    assert "before every command" not in security
    assert "It runs for Claude Code only. Other runners do not load it." in security


def test_2030_publication_credential_reach_matches_security_page() -> None:
    threat = _flat("docs/threat-model.md")
    assert "reaches only" not in threat
    assert "Git in the operator's own project checkout" in threat
    assert "operator's own project checkout" in _flat("docs/security.md")


def test_2030_resource_caps_are_scoped_to_containers() -> None:
    threat = _flat("docs/threat-model.md")
    assert "These caps and this watchdog apply to container runs only." in threat


def test_2030_anchor_is_computed_on_the_ledger_host() -> None:
    workflow = _read(".github/workflows/audit-anchor.yml")
    # The anchor job runs only in the private repository, on its self-hosted
    # runner (the ledger host); anywhere else it is skipped, never hosted.
    assert "if: needs.visibility.outputs.visibility == 'private'" in workflow
    assert (
        "runs-on: ${{ needs.visibility.outputs.visibility == 'private'"
        " && 'self-hosted' || 'ubuntu-latest' }}"
    ) in workflow
    audit = _flat("docs/audit.md")
    assert "computed on the ledger host, not by an independent witness" in audit
    for rel in ("docs/threat-model.md", "SECURITY.md"):
        text = _flat(rel)
        for stale in ("externally anchorable", "anchored outside the host"):
            assert stale not in text, (rel, stale)


def test_2030_git_install_names_the_missing_web_bundle() -> None:
    ignored = _read(".gitignore")
    assert "kew/src/kew/serve/web/" in ignored
    from kew.serve.bundle import missing_build_html

    assert "web UI is not built" in missing_build_html()
    for rel in ("README.md", "docs/getting-started.md"):
        # Unquote the README's blockquote before joining lines.
        text = " ".join(re.sub(r"^\s*>\s?", "", _read(rel), flags=re.M).split())
        assert "(with the web `serve` extra)" not in text, rel
        assert "A git install does not include the web UI bundle" in text, rel


def test_2030_kew_review_is_not_called_an_independent_reviewer() -> None:
    philosophy = _flat("docs/philosophy.md")
    assert "puts an independent agent on the diff" not in philosophy
    assert "address the review comments already on a PR" in philosophy


@pytest.mark.policy
def test_2030_flight_recorder_status_matches_frontend() -> None:
    loader = _read("frontend/src/routes/(factory)/runs/[id]/+page.ts")
    assert "fetchFlight(" in loader
    consumers = [
        p
        for p in (REPO_ROOT / "frontend/src").rglob("*")
        if p.suffix in {".ts", ".svelte"}
        and ".test." not in p.name
        and p.name != "api.ts"
        and re.search(r"\b(fetchTimeline|streamEvents)\(", p.read_text())
    ]
    assert consumers == []
    for rel in ("README.md", "docs/web/index.md", "docs/philosophy.md"):
        text = _flat(rel)
        assert "being repaired" not in text, rel
        assert "consumer is pending" not in text, rel
    assert "no web page reads them yet" in _flat("docs/web/index.md")


def test_2030_web_nav_destinations_match_the_header() -> None:
    """The docs' two nav tables are the header's two lists, in order.

    FUI-26 retired the eight-destination left rail: every page moved under the
    56px header, whose three tabs and "More" menu are the whole navigation. The
    docs describe both, so both are read back from nav.ts rather than trusted.
    """
    nav = _read("frontend/src/lib/shell/nav.ts")
    web = _read("docs/web/index.md")

    def declared(name: str) -> list[str]:
        body = nav.split(f"export const {name}: Destination[] = [", 1)[1]
        body = body.split("];", 1)[0]
        return re.findall(r"\{ label: '([^']+)', href: '[^']+' \}", body)

    def documented(intro: str) -> list[str]:
        # The table is the second block after the sentence introducing it.
        table = web.split(intro)[1].split("\n\n", 2)[1]
        return re.findall(r"^\| \*\*([A-Za-z]+)\*\*", table, re.M)

    tabs = declared("FACTORY_TABS")
    assert tabs == ["Work", "Monitor", "Epics"]
    assert documented("The three tabs are the factory:") == tabs

    # The More menu draws its destinations in labelled groups (D40, #3470), and
    # MORE_DESTINATIONS is those groups flattened, so the groups are the source.
    assert "MORE_DESTINATIONS: Destination[] = MORE_GROUPS.flatMap" in nav
    groups_src = nav.split("export const MORE_GROUPS: DestinationGroup[] = [", 1)[1]
    groups_src = groups_src.split("\n];", 1)[0]
    groups = [
        (label, re.findall(r"\{ label: '([^']+)', href: '[^']+' \}", items))
        for label, items in re.findall(
            r"label: '([^']+)',\s*items: \[(.*?)\]", groups_src, re.S
        )
    ]
    assert [label for label, _ in groups] == ["Factory", "History", "Settings"]
    more = [item for _, items in groups for item in items]
    # Eight since #2298 added Health; nine since #3019 added Prompts; eight
    # again since #3454 removed the legacy Console. The count is asserted as
    # well as the list so a destination that is added to BOTH sides without
    # being thought about still trips a deliberate line.
    assert len(more) == 8
    assert documented("keep their own older layout:") == more
    # Each row names the group the menu draws it under.
    table = web.split("keep their own older layout:")[1].split("\n\n", 2)[1]
    rows = re.findall(r"^\| \*\*([A-Za-z]+)\*\* \| ([A-Za-z]+) \|", table, re.M)
    assert rows == [(item, label) for label, items in groups for item in items]
    # The rail is gone from the code, so it must be gone from the prose too.
    assert "left rail" not in web
    assert "export const DESTINATIONS" not in nav
    # And the cutover's redirects are what the startup link depends on.
    assert "redirects to" in web
    assert "/work?filter=needs-you" in web


def test_2030_local_models_are_labelled_unpriced() -> None:
    scorecard = _read("frontend/src/routes/(factory)/scorecard/+page.svelte")
    assert "local · free" not in scorecard
    assert "local · unpriced" in scorecard
    assert "Local or unpriced does not mean free." in _flat(
        "docs/design/2026-08-17-kew-product-charter.md"
    )
    assert '"local · unpriced" badge' in _read("docs/web/index.md")


# The only "merg" words the hero may draw: the name of the gate, and the two
# config keys the gate-edits row names as its setting (docs/proof.md). Each is
# a name, never a claim that anything merged.
_HERO_MERGE_NAMES = ("merge verify", "merge_gate_paths", "merge_queue")


def _hero_merge_words(text: str) -> list[str]:
    """Every word starting "merg" in ``text`` outside the allowed names."""
    for name in _HERO_MERGE_NAMES:
        text = re.sub(re.escape(name), " ", text, flags=re.I)
    return re.findall(r"\bmerg\w*", text, re.I)


# One tokenizer: comments and the three string forms are alternatives of one
# pattern, scanned left to right, so whichever starts first wins. A "//" inside
# a string stays in the string, and a quote inside a comment opens nothing.
_JS_TOKEN = re.compile(
    r"//[^\n]*"
    r"|/\*.*?\*/"
    r"""|'((?:[^'\\\n]|\\.)*)'|"((?:[^"\\\n]|\\.)*)"|`((?:[^`\\]|\\.)*)`""",
    re.S,
)


def _js_string_literals(source: str) -> list[str]:
    """Every string literal in ``source`` outside its comments: single-quoted,
    double-quoted and template literals, each with its escapes."""
    found = []
    for m in _JS_TOKEN.finditer(source):
        strings = [g for g in m.groups() if g is not None]
        if strings:
            found.append(strings[0])
    return found


def _proof_export_strings(proof: str, name: str, seen: set[str]) -> list[str]:
    """The string literals an export of proof.js holds, following every other
    PROOF_ export it is built from (PROOF_ALWAYS filters PROOF_CHANNELS)."""
    if name in seen:
        return []
    seen.add(name)
    found = re.search(
        rf"^export const {name}\b(.*?)(?=^export |^/\*\*|\Z)", proof, re.M | re.S
    )
    assert found is not None, f"proof.js has no export {name}"
    body = found.group(1)
    strings = _js_string_literals(body)
    for other in sorted(set(re.findall(r"\bPROOF_[A-Z_]+\b", body)) - {name}):
        strings += _proof_export_strings(proof, other, seen)
    return strings


def test_2030_www_hero_never_claims_a_merge() -> None:
    # The hero's words are its drawn markup and every string it imports from
    # proof.js (the rule, the rows, the folded row's summary, the doc links).
    # None of them may say anything about merging; the only "merg" words
    # allowed are the names in _HERO_MERGE_NAMES.
    hero = _read("www/src/lib/Hero.svelte")
    script, rest = hero.split("</script>", 1)
    markup = re.sub(r"<!--.*?-->", " ", rest.split("<style>", 1)[0], flags=re.S)
    assert "<h1>" in markup
    imported = re.search(r"import \{([^}]*)\} from './data/proof';", script)
    assert imported is not None
    names = [n.strip() for n in imported.group(1).split(",") if n.strip()]
    required = {"PROOF_RULE", "PROOF_ALWAYS", "PROOF_OPT_IN", "PROOF_OPT_IN_SUMMARY"}
    assert required <= set(names)
    proof = _read("www/src/lib/data/proof.js")
    seen: set[str] = set()
    drawn = [s for name in names for s in _proof_export_strings(proof, name, seen)]
    assert any("never defines its own proof" in s for s in drawn)
    assert any("Its git repository" in s for s in drawn)
    assert _hero_merge_words(markup) == []
    assert _hero_merge_words("\n".join(drawn)) == []


@pytest.mark.parametrize(
    "sentence",
    [
        "kew will merge it after review",
        "Agents merged 159 pull requests.",
        "Merging is automatic once the reviewer passes it.",
    ],
)
def test_2030_www_hero_merge_rule_refuses_merge_claims(sentence: str) -> None:
    assert _hero_merge_words(sentence) != []
    assert _hero_merge_words("Runs in the merge verify, with merge_gate_paths.") == []


def test_2030_roadmap_drops_sovereign_dispatch_and_follows_charter() -> None:
    for rel in ("docs/roadmap.md", "README.md"):
        assert "sovereign" not in _read(rel).lower(), rel
    roadmap = _flat("docs/roadmap.md")
    assert "Local or unpriced does not mean free." in roadmap
    assert "It does not make kew a general infrastructure orchestrator." in roadmap
    assert "fine-tune" not in roadmap
    assert "Model-lifecycle truth belongs to the external `model-eol` product." in (
        roadmap
    )


def test_2030_create_kew_codex_block_matches_native_posture() -> None:
    from kew.sandbox import runner_native_containment

    assert runner_native_containment("codex") is not None
    template = _read("create-kew/templates/kew.toml")
    block = template.split("# Official Codex is explicit-only.", 1)[1].split("\n\n", 1)[
        0
    ]
    assert "enabled = false" not in block
    assert "needs no container setup and no [sandbox] change" in " ".join(block.split())


def _approval_hold_notice_nodes(tree: ast.AST) -> set[int]:
    """The ids of the hold notice's string and its pieces, if *tree* has it.

    The notice is the f-string `notify_approval_hold` assigns to `message`;
    only that string, not its module, may name the approval workflow.
    """
    for func in ast.walk(tree):
        if not (
            isinstance(func, ast.FunctionDef) and func.name == "notify_approval_hold"
        ):
            continue
        for stmt in ast.walk(func):
            if (
                isinstance(stmt, ast.Assign)
                and [ast.unparse(t) for t in stmt.targets] == ["message"]
                and isinstance(stmt.value, ast.JoinedStr)
            ):
                return {id(node) for node in ast.walk(stmt.value)}
    return set()


def test_2030_only_the_hold_notice_may_name_the_approval_workflow() -> None:
    tree = ast.parse(
        "def notify_approval_hold(n):\n"
        "    message = f'#{n}: needs `approval.yml` installed'\n"
        "    hint = 'copy approval.yml'\n"
    )
    notice = _approval_hold_notice_nodes(tree)
    named = [
        text
        for node in ast.walk(tree)
        if id(node) not in notice
        and (text := source_ast.template(node)) is not None
        and "approval.yml" in text
    ]
    assert named == ["copy approval.yml"]


@pytest.mark.policy
def test_2030_comment_approval_names_its_workflow() -> None:
    assert (REPO_ROOT / ".github/workflows/approval.yml").exists()
    installer = REPO_ROOT / "create-kew/index.js"
    assert "approval.yml" not in installer.read_text(encoding="utf-8")
    # kew's own setup could only name the workflow in a string value. The one
    # exception is the approval-hold notice, which names it as what `/approve`
    # needs and never installs it; any other string naming it still trips.
    for path in sorted((REPO_ROOT / "kew/src/kew").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        notice = _approval_hold_notice_nodes(tree)
        for node in ast.walk(tree):
            if id(node) in notice:
                continue
            text = source_ast.template(node)
            assert text is None or "approval.yml" not in text, path
    readme = _flat("README.md")
    assert "neither `kew setup` nor `create-kew` installs it" in readme


#: The facts `operator_merge._reprove_gates` re-proves inside the queue lease,
#: as the distinctive token each of its three prose descriptions must carry.
OPERATOR_MERGE_REPROVEN = (
    "open state",
    "head",
    "base branch",
    "CI gates",
    "live-work state",
    "hold",
    "review verdict",
)

#: The gate helpers `_reprove_gates` calls. Extending the set means extending
#: every prose description of it — which is what this pairing enforces.
OPERATOR_MERGE_REPROOF_GATES = frozenset(
    {
        "_open_gate",
        "_bound_head",
        "_base_gate",
        "_ci_gate",
        "_live_node_facts",
        "_hold_gate",
        "_verdict_gate",
    }
)


@pytest.mark.policy
def test_2203_the_in_lease_re_proof_set_is_documented_everywhere() -> None:
    """#2203: three places describe what `kew epic merge` re-proves inside the
    queue lease — the module docstring, the CLI help and docs/cli/epic.md. A
    fact added to `_reprove_gates` and to only one of them is a guarantee the
    operator cannot read (the stale-docs finding on PR #2218 round 4)."""
    from kew.cli.epic import epic_merge
    from kew.graph.driver import operator_merge

    reprove = source_ast.definition(operator_merge, "_reprove_gates")
    called = {
        node.func.id
        for node in ast.walk(reprove)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called == set(OPERATOR_MERGE_REPROOF_GATES), (
        "the in-lease re-proof set changed — update the module docstring, the "
        "`kew epic merge` help, docs/cli/epic.md and this expectation together"
    )

    anchor = "another actor can have moved"
    prose = {
        "the operator_merge module docstring": operator_merge.__doc__ or "",
        "the `kew epic merge` help": epic_merge.__doc__ or "",
        "docs/cli/epic.md": _read("docs/cli/epic.md"),
    }
    for where, text in prose.items():
        assert anchor in text, f"{where} no longer describes the re-proof set"
        paragraph = " ".join(text.split(anchor, 1)[1].split("\n\n", 1)[0].split())
        for fact in OPERATOR_MERGE_REPROVEN:
            assert fact in paragraph, f"{where} does not name the re-proven {fact}"


def _prose(text: str) -> str:
    """*text* as one line, TOML comment markers dropped, for phrase checks."""
    return " ".join(text.replace("#", " ").split())


def test_security_page_says_the_reviewer_is_not_safe_on_untrusted_fork_prs() -> None:
    """The reviewer's boundary is the agent's own uid (#2422), so the pages an
    operator reads before turning on `[review] adversarial` say plainly that
    it is not yet safe on pull requests from untrusted forks: the security
    page's residual boundary, and the key's own line in the configuration
    reference."""
    warning = (
        "runs the PR's code as the agent's user, so it is not yet safe on "
        "pull requests from untrusted forks"
    )
    security = _prose(_read("docs/security.md"))
    boundary = security.split("Residual boundary: ", 1)[1].split("###", 1)[0]
    assert warning in boundary, "docs/security.md: reviewer boundary"

    configuration = _read("docs/configuration.md")
    review = configuration.split("\n[review]\n", 1)[1].split("\n[", 1)[0]
    adversarial = review.split("adversarial = false", 1)[1].split(" = ", 1)[0]
    assert warning in _prose(adversarial), "docs/configuration.md: [review] adversarial"


class TestWhatWorksWithLinear:
    """#1117 launch rulings (2026-09-23): the public docs state what the Linear
    rail supports and which surfaces stay GitHub-only, and the code agrees."""

    SECTION = "### What works with Linear"

    def _section(self) -> str:
        text = _read("docs/configuration.md")
        assert self.SECTION in text
        return text.split(self.SECTION, 1)[1].split("\n## ", 1)[0]

    def test_no_page_claims_live_certification_is_pending(self) -> None:
        for page in ("README.md", "docs/linear-webhooks.md"):
            text = " ".join(_read(page).split())
            assert "certification remains pending" not in text, page
            assert "certification is still pending" not in text, page
            assert "configuration.md#what-works-with-linear" in text, page
        readme = _read("README.md")
        assert "have run end to end against a live Linear workspace" in readme

    def test_webhooks_are_not_claimed_as_live_verified(self) -> None:
        section = " ".join(self._section().split())
        assert "Webhook delivery has automated coverage only" in section
        webhooks = " ".join(_read("docs/linear-webhooks.md").split())
        assert "have not been exercised against a live workspace" in webhooks

    def test_failure_classification_matches_the_client(self) -> None:
        """#2020: the bullet lists what is retried and what fails fast; the
        client's status classifier must agree (tick-level proof lives in
        tests/test_linear_outage_tick.py)."""
        from kew.linear.client import (
            _AUTH_HTTP_STATUS,
            LinearClient,
            LinearUnavailableError,
            _is_transient_status,
        )

        section = " ".join(self._section().split())
        assert "Every Linear API failure ends the tick with a one-line" in section
        assert "never a traceback" in section
        assert (
            "Reads retry outages (a DNS or connect failure, a timeout, a reset, "
            "any 5xx, an edge 429) and then report `linear-unavailable`"
        ) in section
        assert (
            "A write that gets a 5xx or edge-429 reply is not retried, because it "
            "may have applied; the tick reports `linear-outcome-unknown`"
        ) in section
        assert (
            "TLS, authentication, other 4xx and GraphQL errors fail fast as "
            "`linear-auth` or `linear-error`"
        ) in section
        for status in (429, 500, 502, 503, 504, 520, 522, 599):
            assert _is_transient_status(status), status
        for status in (400, 401, 403, 404, 422):
            assert not _is_transient_status(status), status
        assert set(_AUTH_HTTP_STATUS) == {401, 403}

        # Reads retry a 500; a write with a 500 reply is sent once.
        for query, sends in (("query { viewer { id } }", 3), ("mutation M { m }", 1)):
            sent: list[bytes] = []

            def transport(
                _url: str, payload: bytes, _headers: dict, sent: list = sent
            ) -> tuple:
                sent.append(payload)
                return 500, b"<html>500</html>"

            client = LinearClient("k", transport=transport, sleep=lambda _s: None)
            with pytest.raises(LinearUnavailableError):
                client.execute(query)
            assert len(sent) == sends, query

    def test_per_issue_commands_take_github_issue_numbers(self) -> None:
        import typer

        from kew.cli import app

        section = self._section()
        assert "`kew dispatch`, `kew attach` and `kew approve` take" in " ".join(
            section.split()
        )
        commands = typer.main.get_command(app).commands  # type: ignore[attr-defined]
        for name, param in (
            ("dispatch", "issue"),
            ("attach", "issue"),
            ("approve", "issue"),
            ("deny", "issue"),
        ):
            params = {p.name: p for p in commands[name].params}
            assert params[param].type.name == "integer", name

    def test_epic_item_verbs_accept_linear_ids(self) -> None:
        from kew.cli import epic as epic_cli
        from kew.ids import validate_item_id

        section = " ".join(self._section().split())
        assert (
            "`kew epic retry`, `kew epic unhold`, `kew epic continue` and "
            "`kew epic abandon`" in section
        )
        assert validate_item_id("SFD-1", "linear") == "SFD-1"
        # Each verb parses its id through the provider-aware validator: read
        # off the syntax tree, so what counts is the call, not its spelling.
        for verb, validator in (
            ("epic_retry", "validate_item_id"),
            ("epic_unhold", "validate_item_id"),
            ("epic_abandon", "validate_item_id"),
            ("epic_continue", "_validate_continue_item"),
        ):
            assert source_ast.calls(source_ast.definition(epic_cli, verb), validator), (
                verb
            )
        continue_item = source_ast.definition(epic_cli, "_validate_continue_item")
        assert source_ast.calls(continue_item, "validate_item_id")

    def test_linear_writes_no_labels_and_never_the_review_state(self) -> None:
        """The GitHub-only list says kew writes no Linear labels and never moves
        an item into the review state; drive every write the tracker has."""
        from kew.config import DispatchConfig
        from kew.linear.tracker import LinearTracker

        section = " ".join(self._section().split())
        assert "kew writes no labels on a Linear item" in section
        assert "never moves an item into the review state" in section

        states = [
            {"id": "s-todo", "name": "Todo", "type": "unstarted"},
            {"id": "s-review", "name": "In Review", "type": "started"},
            {"id": "s-progress", "name": "In Progress", "type": "started"},
            {"id": "s-done", "name": "Done", "type": "completed"},
        ]
        writes: list[dict[str, object]] = []

        class _Client:
            mutation_outcome_ambiguous = False

            def execute(self, query: str, variables: dict | None = None) -> dict:
                if "query TeamStates" in query:
                    return {"teams": {"nodes": [{"states": {"nodes": states}}]}}
                if "viewer" in query:
                    return {"viewer": {"id": "kew-member"}}
                writes.append(dict(variables or {}))
                if "mutation CommentCreate" in query:
                    return {"commentCreate": {"success": True}}
                return {"issueUpdate": {"success": True, "issue": {}}}

        config = DispatchConfig()
        config.tracker.type = "linear"
        config.tracker.linear.team = "ENG"
        tracker = LinearTracker(config, _Client())  # type: ignore[arg-type]
        tracker.claim("ENG-1")
        tracker.comment("ENG-1", "note")
        tracker.close("ENG-1")
        tracker.apply_no_change_outcome("ENG-1", body="no change", close=True)

        inputs = [w["input"] for w in writes]
        assert inputs, "no Linear write was exercised"
        for payload in inputs:
            assert isinstance(payload, dict)
            assert not any("label" in key.lower() for key in payload), payload
            assert payload.get("stateId") != "s-review", payload
        assert {"stateId": "s-progress", "assigneeId": "kew-member"} in inputs


class TestHostsDoc:
    """docs/hosts.md exists, is reachable, and quotes the recipe accurately."""

    def test_page_exists_and_is_in_the_nav(self) -> None:
        assert (REPO_ROOT / "docs/hosts.md").is_file(), (
            "docs/hosts.md must exist — it is the operator page for the "
            "machines kew ships verification jobs to"
        )
        assert "hosts.md" in _mkdocs_nav_block(), (
            "docs/hosts.md must be wired into the mkdocs.yml nav"
        )

    def test_page_is_linked_from_security(self) -> None:
        assert "(hosts.md)" in _read("docs/security.md"), (
            "docs/security.md must link docs/hosts.md: the host contract's "
            "credential posture is a security claim"
        )

    def test_the_cli_page_is_registered(self) -> None:
        assert "cli/hosts.md" in _mkdocs_nav_block(), (
            "docs/cli/hosts.md must be registered in the mkdocs.yml nav"
        )
        assert "hosts.md" in _read("docs/cli/index.md"), (
            "kew hosts provision must appear in the CLI reference table"
        )

    def test_it_names_the_recipe_that_ships(self) -> None:
        from kew.hosts import recipe_path

        recipe = recipe_path()
        relative = str(recipe.relative_to(REPO_ROOT))
        for page in ("docs/hosts.md", "docs/cli/hosts.md"):
            assert relative.rsplit("/", 1)[0] in _read(page), (
                f"{page} must name the directory the shipped recipe lives in"
            )
        assert relative in _read("docs/hosts.md"), (
            "docs/hosts.md must name the shipped recipe by path"
        )

    def test_the_pins_it_quotes_are_the_recipe_pins(self) -> None:
        from kew.hosts import recipe_path

        script = recipe_path().read_text(encoding="utf-8")
        page = _read("docs/hosts.md")
        for claim, pin in (("playwright-core", "1.63.0"), ("Node", "22")):
            assert f'"{pin}"' in script, (
                f"the recipe must pin {claim} to {pin}, or the doc is wrong"
            )
            assert pin in page, f"docs/hosts.md must quote the {claim} pin {pin}"

    def test_the_scanner_pin_it_quotes_is_the_one_shipped_pin(self) -> None:
        """#2462 — the recipe installed no trivy and the gate pinned one.

        The page quotes a version, so a stale quote here is an operator
        installing the wrong scanner by hand. The value is read from the shipped
        pin file, which is the only place the recipe and the gate read it from.
        """
        from kew.hosts import pin_path, pinned_trivy_version

        page = _read("docs/hosts.md")
        assert pinned_trivy_version() in page, (
            "docs/hosts.md must quote the pinned trivy version the recipe "
            "installs and the agent-image gate demands"
        )
        assert str(pin_path().relative_to(REPO_ROOT)) in page, (
            "docs/hosts.md must name the pin file both of them read"
        )

    def test_the_self_check_marker_it_quotes_is_the_real_one(self) -> None:
        from kew.hosts import TOOL_MARKER

        for page in ("docs/hosts.md", "docs/cli/hosts.md"):
            assert TOOL_MARKER in _read(page), (
                f"{page} must quote the self-check marker {TOOL_MARKER} verbatim"
            )


class TestPlacementRunbook:
    """docs/hosts.md's placement runbook: every key, name and category is real.

    Node 7 of #2338 made this page the operator's instructions for putting an
    agent run on a VM — the one path that sends a credential to another
    machine. A policy value the config loader would refuse, a credential name
    the runner does not declare, or an error category nothing raises costs a
    day of placed runs rather than one wrong sentence.
    """

    PAGE = "docs/hosts.md"

    def test_the_runbook_sections_are_on_the_page(self) -> None:
        page = _read(self.PAGE)
        for heading in (
            "## Placing agent runs",
            "### How to place runs",
            "### The credential exception",
            "### Spot instances, and what a pre-emption looks like",
            "### Falling back to local",
            "### What to measure",
        ):
            assert heading in page, f"{self.PAGE} must carry the {heading!r} section"
        assert "](#the-credential-exception)" in page, (
            "the page's own pointer to the exception must resolve: mkdocs "
            "validates anchors and the build is --strict"
        )

    def test_every_policy_it_names_is_one_the_loader_accepts(self) -> None:
        from pydantic import ValidationError

        from kew.config import (
            PLACEMENT_GCP,
            PLACEMENT_LOCAL,
            PLACEMENT_PREFER_GCP,
            SandboxConfig,
        )

        page = _read(self.PAGE)
        for policy in (PLACEMENT_LOCAL, PLACEMENT_GCP, PLACEMENT_PREFER_GCP):
            assert f'"{policy}"' in page, (
                f"docs/hosts.md must quote the {policy!r} placement policy"
            )
            assert SandboxConfig(placement=policy).placement_policy() == policy
        with pytest.raises(ValidationError):
            SandboxConfig(placement="prefer-local")

    def test_the_run_kinds_the_pin_example_names_are_real(self) -> None:
        from kew.run_store import KNOWN_RUN_KINDS

        page = _read(self.PAGE)
        for kind in ("dispatch", "adversarial_review", "review_remediation", "ci_fix"):
            assert f"`{kind}`" in page, f"the pin example must name {kind}"
            assert kind in KNOWN_RUN_KINDS, (
                f"{kind} is not a run kind sandbox.hosts would accept"
            )

    def test_the_two_caps_it_quotes_are_the_two_caps(self) -> None:
        from pydantic import ValidationError

        from kew.config import DispatchConfig, FleetConfig, GcpHostConfig

        page = _read(self.PAGE)
        assert "[fleet] max_concurrent" in page
        assert "max_concurrent" in FleetConfig.model_fields
        assert DispatchConfig().hosts.gcp.max_runs == 1, (
            "the page says max_runs is 1 per instance"
        )
        with pytest.raises(ValidationError):
            GcpHostConfig(project="p", zone="z", instance="i", max_runs=2)

    def test_the_policy_really_has_no_environment_override(self) -> None:
        from kew.config import _ENV_OVERRIDES

        assert "no environment variable for the policy" in _read(self.PAGE)
        assert not [
            name for name, target in _ENV_OVERRIDES.items() if target[0] == "sandbox"
        ], "an env override for [sandbox] exists, so the page is wrong"

    def test_the_credential_names_it_quotes_are_the_runner_declaration(self) -> None:
        from kew.sandbox import runner_credential_env_vars

        declared = set(runner_credential_env_vars("claude-code"))
        assert declared, "claude-code must declare a model credential"
        page = _read(self.PAGE)
        for name in declared:
            assert f"`{name}`" in page, (
                f"claude-code declares {name}, so the exception the page "
                "describes admits it and the page must name it"
            )

    def test_only_a_dispatch_run_carries_the_credential_it_describes(self) -> None:
        from kew.hosts.errors import CredentialVariableRefused
        from kew.hosts.remote_dispatch import (
            DISPATCH_RUN_KIND,
            permit_model_credentials,
        )

        assert f"`{DISPATCH_RUN_KIND}`" in _read(self.PAGE)
        permitted = permit_model_credentials(
            {"CLAUDE_CODE_OAUTH_TOKEN": "secret-value"},
            runner="claude-code",
            run_kind=DISPATCH_RUN_KIND,
        )
        assert [str(credential) for credential in permitted] == [
            "CLAUDE_CODE_OAUTH_TOKEN"
        ]
        for refused, kind in (
            ({"CLAUDE_CODE_OAUTH_TOKEN": "v"}, "adversarial_review"),
            ({"GH_TOKEN": "v"}, DISPATCH_RUN_KIND),
        ):
            with pytest.raises(CredentialVariableRefused):
                permit_model_credentials(refused, runner="claude-code", run_kind=kind)

    def test_the_audit_event_it_quotes_is_the_one_a_launch_writes(self) -> None:
        from kew.hosts.remote_dispatch import (
            REMOTE_LAUNCH_EVENT,
            RemoteLaunch,
            record_remote_launch,
        )

        launch = RemoteLaunch(
            host="gcp",
            instance="agent-1",
            image="kew-agent:docker-d148816b388f",
            runner="claude-code",
            credential_names=("CLAUDE_CODE_OAUTH_TOKEN",),
        )
        written: dict[str, object] = {}

        class _Ledger:
            def append_audit_event(self, **kwargs: object) -> int:
                written.update(kwargs)
                return 1

        record_remote_launch(_Ledger(), launch, run_id="r1")  # type: ignore[arg-type]

        page = _read(self.PAGE)
        quoted = [
            json.loads(block)
            for block in re.findall(r"```json\n(.*?)```", page, re.S)
            if REMOTE_LAUNCH_EVENT in block
        ]
        assert len(quoted) == 1, (
            "docs/hosts.md must quote the launch audit event exactly once"
        )
        event = quoted[0]
        assert event["event"] == written["event"] == REMOTE_LAUNCH_EVENT
        assert event["actor_type"] == written["actor_type"]
        assert event["actor_id"] == written["actor_id"] == launch.instance
        assert set(event["payload"]) == set(launch.audit_payload()), (
            "the payload the page shows must be the payload a launch records"
        )
        assert event["payload"]["credentials"] == ["CLAUDE_CODE_OAUTH_TOKEN"]

    def test_the_credential_value_is_nowhere_in_what_is_recorded(self) -> None:
        from kew.hosts.remote_dispatch import ModelCredential, RemoteLaunch

        credential = ModelCredential(name="CLAUDE_CODE_OAUTH_TOKEN", value="sk-secret")
        launch = RemoteLaunch(
            host="gcp",
            instance="agent-1",
            image="img",
            runner="claude-code",
            credential_names=(credential.name,),
        )
        assert credential.value not in repr(credential)
        assert credential.value not in json.dumps(launch.audit_payload())
        assert "by name" in _read(self.PAGE)

    def test_the_interruption_it_quotes_is_the_one_kew_raises(self) -> None:
        from kew.hosts import remote_dispatch
        from kew.hosts.remote_dispatch import RemoteRunInterrupted

        page = _read(self.PAGE)
        assert RemoteRunInterrupted.error_category in page, (
            "the page must name the error category a pre-emption is reported as"
        )
        # The message as the parser joins it: one f-string template, however
        # its pieces are wrapped across lines.
        relay = source_ast.definition(remote_dispatch, "relay_remote_session")
        messages = [
            source_ast.template(call.args[0])
            for call in source_ast.calls(relay, "RemoteRunInterrupted")
            if call.args
        ]
        for fragment in ("ended with no exit marker after", "is NOT a success"):
            assert any(fragment in (message or "") for message in messages), (
                f"relay_remote_session no longer says {fragment!r}, so the "
                "sample the page quotes is stale"
            )
            assert fragment in page

    def test_a_pre_empted_instance_is_one_kew_can_start_again(self) -> None:
        from kew.hosts.gcp import STARTABLE_STATUSES

        assert "--instance-termination-action=STOP" in _read(self.PAGE)
        assert "TERMINATED" in STARTABLE_STATUSES, (
            "the page tells the operator to have a pre-emption STOP the "
            "instance because the next placement starts it again"
        )

    def test_the_fallback_table_matches_what_a_placement_does(self) -> None:
        from kew.config import DispatchConfig
        from kew.fleet_placement import LOCAL_SITE, FleetOccupancy, RunSite, place_run

        pool = {
            "hosts": {
                "gcp": {
                    "project": "proj",
                    "zone": "us-central1-a",
                    "instances": ["agent-1"],
                }
            }
        }
        full = FleetOccupancy({RunSite("gcp", "agent-1"): 1})
        prefer = DispatchConfig(sandbox={"placement": "prefer-gcp"}, **pool)
        pinned = DispatchConfig(sandbox={"placement": "gcp"}, **pool)
        assert place_run(prefer, full).site == LOCAL_SITE, (
            "the page says prefer-gcp places here when every instance is full"
        )
        assert place_run(pinned, full).waits, (
            "the page says a gcp pin waits rather than quietly landing here"
        )
        assert place_run(DispatchConfig(sandbox={"placement": "local"}), full).site == (
            LOCAL_SITE
        )
        assert (
            place_run(
                DispatchConfig(
                    sandbox={"placement": "gcp", "hosts": {"dispatch": "local"}}, **pool
                ),
                FleetOccupancy({}),
                run_kind="dispatch",
            ).site
            == LOCAL_SITE
        ), "the page offers a per-kind pin as the smaller fallback"

    def test_the_ledger_fields_it_points_at_are_exported(self) -> None:
        from kew.run_store import _EXPORT_FIELDS

        page = _read(self.PAGE)
        for field in (
            "started_at",
            "finished_at",
            "duration_seconds",
            "run_host",
            "run_host_instance",
            "error_category",
        ):
            assert f"`{field}`" in page, (
                f"the measurement table must name {field}, the column it reads"
            )
            assert field in _EXPORT_FIELDS, (
                f"kew export does not carry {field}, so the measurement "
                "recipe the page gives cannot be followed"
            )

    def test_the_exception_is_stated_on_the_security_page_too(self) -> None:
        from kew.hosts.remote_dispatch import REMOTE_LAUNCH_EVENT

        security = _read("docs/security.md")
        assert "Linear, Google or cloud credential" in security, (
            "the host boundary claim must be the true one: a model credential "
            "does reach a dispatch placed on a host"
        )
        assert "or model credential, and it is never asked to publish" not in security
        assert REMOTE_LAUNCH_EVENT in security
        assert "hosts.md#the-credential-exception" in security


class TestOpenShellRunbook:
    """docs/openshell.md: reachable, and every command and key it names is real.

    The runbook is the one page an operator follows without reading the design,
    so a command it names that does not exist — or a config key the loader would
    refuse — costs a setup attempt rather than a wrong sentence (#2321).
    """

    PAGE = "docs/openshell.md"

    def test_page_exists_and_is_in_the_nav(self) -> None:
        assert (REPO_ROOT / self.PAGE).is_file(), (
            "docs/openshell.md must exist — it is the operator runbook for the "
            "openshell containment runtime"
        )
        assert "openshell.md" in _mkdocs_nav_block(), (
            "docs/openshell.md must be wired into the mkdocs.yml nav"
        )

    def test_it_is_linked_from_security_and_the_doctor_page(self) -> None:
        assert "(openshell.md)" in _read("docs/security.md"), (
            "docs/security.md must link the runbook: the runtime's egress and "
            "credential posture are security claims"
        )
        assert "../openshell.md" in _read("docs/cli/doctor.md"), (
            "docs/cli/doctor.md must link the runbook — the checks name the "
            "steps it writes out"
        )

    def test_every_kew_command_it_names_exists(self) -> None:
        """No ghost commands: each `kew ...` line resolves in the real CLI."""
        from typer.main import get_command

        from kew.cli import app

        root = get_command(app)
        named = set(
            re.findall(r"^kew ([a-z][a-z-]*(?: [a-z][a-z-]*)?)", _read(self.PAGE), re.M)
        )
        assert named, "the runbook must name the kew commands it tells you to run"
        for phrase in sorted(named):
            command = root
            for part in phrase.split():
                child = command.get_command(None, part)  # type: ignore[union-attr]
                if child is None:
                    break
                command = child
            else:
                continue
            raise AssertionError(
                f"docs/openshell.md names a ghost command: kew {phrase}"
            )

    def test_it_names_the_certify_command_with_its_real_options(self) -> None:
        page = _read(self.PAGE)
        assert "kew sandbox certify --runtime openshell --runner" in page, (
            "the runbook must give the exact certify invocation admission asks for"
        )
        assert "24 hours" in page, (
            "the runbook must say the certification expires, or step 4 reads "
            "like a one-off"
        )

    def test_every_config_key_it_shows_loads(self, tmp_path) -> None:
        """Each `toml` block in the page loads through the real config loader.

        Block by block rather than merged: the page shows `[sandbox]` twice on
        purpose (openshell in step 2, back to docker in teardown), and a merged
        file would fail on the duplicate table instead of on a wrong key.
        """
        from kew.config import load_config

        blocks = re.findall(r"```toml\n(.*?)```", _read(self.PAGE), re.S)
        assert len(blocks) >= 4, "the runbook must show the config each step needs"
        seen: dict[str, object] = {}
        for index, block in enumerate(blocks):
            path = tmp_path / f"kew-{index}.toml"
            path.write_text('[project]\nrepo = "o/r"\n' + block)
            config = load_config(path)
            seen.setdefault("version", config.sandbox.openshell.version)
            if config.sandbox.openshell.providers:
                seen["providers"] = config.sandbox.openshell.providers
            if config.sandbox.runtime != "docker":
                seen["runtime"] = config.sandbox.runtime
        assert seen["runtime"] == "openshell"
        assert seen["providers"] == {
            "claude-code": "kew-claude-code",
            "codex": "kew-codex",
        }

    def test_the_version_it_quotes_is_the_pinned_default(self) -> None:
        from kew.config import OpenShellConfig

        pinned = OpenShellConfig().version
        assert f'version = "{pinned}"' in _read(self.PAGE), (
            f"the runbook must quote the pinned CLI version {pinned}"
        )

    def test_it_states_the_install_route_and_why_homebrew_is_refused(self) -> None:
        page = _read(self.PAGE)
        for token in ("sha256sum", "checksums.txt", "tar -xzf"):
            assert token in page, f"the install step must be checksum-verified: {token}"
        flat = " ".join(page.split())
        assert "Homebrew" in flat and "brew service" in flat, (
            "the runbook must say why the Homebrew installer is refused: its "
            "brew-service gateway never read the shell's telemetry setting"
        )
        assert "telemetry setting" in flat, (
            "the refusal must name the setting that does not reach it"
        )

    def test_the_gateway_config_covers_tls_and_telemetry(self) -> None:
        page = _read(self.PAGE)
        for token in (
            "tls:",
            "telemetry:",
            "enabled: false",
            "proposals: false",
            "approval: manual",
        ):
            assert token in page, f"the gateway config step must show {token}"

    def test_the_codex_provider_gets_its_own_home(self) -> None:
        page = _read(self.PAGE)
        assert "CODEX_HOME" in page
        assert "single use" in page, (
            "the runbook must say why the gateway's Codex login cannot share "
            "the host login: refresh tokens are single use"
        )

    def test_it_has_a_teardown_that_deletes_the_providers(self) -> None:
        page = _read(self.PAGE)
        assert "## Teardown" in page
        assert "openshell provider delete" in page, (
            "teardown must delete the providers — a provider on a stopped "
            "gateway still holds a credential"
        )

    def test_the_check_names_it_tabulates_are_the_real_ones(self) -> None:
        """Every row and only the rows `kew doctor` prints."""
        from kew import openshell_doctor

        shipped = {
            value
            for name, value in vars(openshell_doctor).items()
            if name.startswith("CHECK_")
        } | {"openshell-gateway"}
        for page in (self.PAGE, "docs/cli/doctor.md"):
            text = _read(page)
            tabulated = set(re.findall(r"^\| `(openshell-[a-z-]+)`", text, re.M))
            assert tabulated == shipped, (
                f"{page} must tabulate exactly the shipped checks; "
                f"missing {sorted(shipped - tabulated)}, "
                f"stale {sorted(tabulated - shipped)}"
            )

    def test_the_tls_requirement_it_states_is_a_shipped_check(self) -> None:
        """#2321 review: the page required TLS while no check covered it.

        The page writes `tls.enabled` into the gateway config as a requirement,
        so a shipped check has to own it — otherwise the runbook asks for a
        setting `kew doctor` would call this host ready without.
        """
        from kew.openshell import GatewayReport
        from kew.openshell_doctor import CHECK_TLS, TLS_SCHEME, check_tls

        assert callable(check_tls)
        assert "tls" in GatewayReport.__dataclass_fields__
        page = _read(self.PAGE)
        assert f"`{CHECK_TLS}`" in page, (
            f"the runbook must tabulate {CHECK_TLS} next to the TLS it requires"
        )
        assert f'gateway = "{TLS_SCHEME}' in page, (
            "the runbook's own gateway endpoint must be the scheme the check demands"
        )

    def test_it_does_not_promise_a_gateway_held_policy_comparison(self) -> None:
        """#2321 review: no runner-keyed policy is ever registered on a gateway.

        kew hands each sandbox its policy file at create time, so a tabulated
        check that diffed packaged digests against a gateway policy map could
        not pass on a host set up exactly as this page prescribes.
        """
        for page in (self.PAGE, "docs/cli/doctor.md"):
            flat = " ".join(_read(page).split())
            assert "re-register" not in flat, (
                f"{page} must not name a registration step that does not exist"
            )
            assert "openshell-policy-hashes" not in flat, (
                f"{page} must not tabulate a check the gateway cannot answer"
            )

    def test_the_landlock_floor_it_states_is_the_enforced_one(self) -> None:
        from kew.openshell_doctor import MINIMUM_LANDLOCK_ABI

        for page in (self.PAGE, "docs/cli/doctor.md"):
            assert f"Landlock ABI {MINIMUM_LANDLOCK_ABI}" in _read(page), (
                f"{page} must quote the enforced Landlock floor"
            )

    def test_it_names_the_policy_directory_the_checks_read(self) -> None:
        page = _read(self.PAGE)
        assert "docker/openshell/" in page, (
            "the runbook must name the directory the certified policies ship in"
        )
        assert (REPO_ROOT / "docker/openshell").is_dir()


class TestSupervisedDriveDocs:
    """#2621: `kew drive`'s page, its how-it-works section and the charter line
    say what the shipped drive does."""

    PAGE = "docs/cli/drive.md"

    def _section(self) -> str:
        text = _read("docs/how-it-works.md")
        assert "## The supervised drive {#the-supervised-drive}" in text, (
            "docs/how-it-works.md must carry a section on the supervised drive"
        )
        body = text.split("## The supervised drive {#the-supervised-drive}", 1)[1]
        return " ".join(body.split("\n## ", 1)[0].split())

    def test_the_charter_admits_the_supervised_driver_only(self) -> None:
        charter = _flat("docs/design/2026-08-17-kew-product-charter.md")
        assert (
            "A supervised, project-scoped driver that runs while the operator "
            "drives is in scope; autopilot, an always-on service and resident "
            "intake stay out."
        ) in charter

    def test_the_page_states_the_shipped_lock_and_files(self) -> None:
        from kew.cli.drive import DRIVE_LOG, DRIVE_PID
        from kew.driver_cadence import LOCK_HEARTBEAT_STALE_S

        assert LOCK_HEARTBEAT_STALE_S == 5 * 60
        page = _flat(self.PAGE)
        assert "older than five minutes" in page
        assert "stale for five minutes" in self._section()
        assert f"`{DRIVE_LOG}`" in page and f"`{DRIVE_PID}`" in page
        assert "(`dispatch.db`)" in page

    def test_the_page_states_the_shipped_defaults(self) -> None:
        import inspect

        from kew.cli.drive import drive

        params = inspect.signature(drive).parameters
        interval = params["interval"].default.default
        prefix = params["epic_prefix"].default
        page = _read(self.PAGE)
        assert f"(default {interval})" in page
        assert f'(default: "{prefix}")' in page

    def test_the_page_and_section_name_the_merge_rule_records(self) -> None:
        from kew.drive_merge import DECLINED_EVENT, MERGE_TRIGGER, RULE_SIGNED_EVENT

        section = self._section()
        for token in (RULE_SIGNED_EVENT, DECLINED_EVENT, f'trigger: "{MERGE_TRIGGER}"'):
            assert f"`{token}`" in section, token
        audit = _read("docs/audit.md")
        assert "## Standing merge rule attestation" in audit
        assert "## Merging {#merging}" in _read(self.PAGE)

    def test_the_section_links_the_drive_surfaces(self) -> None:
        section = self._section()
        for link in (
            "(cli/drive.md)",
            "(configuration.md#drive-merge)",
            "(audit.md#standing-merge-rule-attestation)",
        ):
            assert link in section, link
        for heading in ("Supervision", "The standing merge rule", "Outage handling"):
            assert f"**{heading}**" in section, heading
        assert "{#drive-merge}" in _read("docs/configuration.md")


class TestPromptProfilesDoc:
    """docs/prompts.md explains prompt profiles and the trust rules behind them."""

    PAGE = "docs/prompts.md"

    def test_page_exists_and_is_in_the_nav(self) -> None:
        assert (REPO_ROOT / self.PAGE).is_file()
        assert "prompts.md" in _mkdocs_nav_block().replace("cli/prompts.md", "")

    def test_it_names_every_role_a_profile_may_apply_to(self) -> None:
        from kew.service.prompts import ROLES

        page = _read(self.PAGE)
        named = [r.role for r in ROLES if r.profile_applies]
        assert named, "the prompt catalog lists no profile role"
        for role in named:
            assert f"`{role}`" in page, role

    def test_it_names_the_heading_the_events_and_the_recorded_column(self) -> None:
        from kew.service import prompt_profiles as profiles

        page = _read(self.PAGE)
        for token in (
            "## Operator instructions — <role> profile v<N>",
            profiles.PROFILE_PROPOSED_EVENT,
            profiles.PROFILE_ACTIVATED_EVENT,
            profiles.PROFILE_DECLINED_EVENT,
            profiles.PROFILE_RETIRED_EVENT,
            "prompt_profile_version_id",
            f"{profiles.MAX_BODY_CHARS}",
        ):
            assert token in page, token

    def test_the_related_pages_link_it(self) -> None:
        for page in (
            "docs/configuration.md",
            "docs/how-it-works.md",
            "docs/security.md",
        ):
            assert "(prompts.md)" in _read(page), page
