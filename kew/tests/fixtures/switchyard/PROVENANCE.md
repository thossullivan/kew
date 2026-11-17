# Switchyard fixture provenance

The `server-help.txt` and `server-version.txt` fixtures are the public stdout of
`switchyard-server --help` and `switchyard-server --version`, run from the
`kew-switchyard` image that `docker/switchyard.Dockerfile` builds from a clean
fetch of upstream revision `46331725daebda02b83781f63bb9c94db9059a1e` with the
repository lockfile and the Rust 1.99.1 toolchain. That revision is a commit
on upstream `main`, not the tagged v0.3.0 release
(`336196f6fbfc97ddc71c1700f6092e564e9f23c2`); it reports version 0.3.0:

```text
docker build -f docker/switchyard.Dockerfile -t kew-switchyard:<tag> .
docker run --rm kew-switchyard:<tag> --help
docker run --rm kew-switchyard:<tag> --version
```

No provider credentials were configured, no provider request was made, and no
proxy was launched for those two.

`metrics.txt` is the `GET /metrics` body of the image built the same way from
the preceding upstream revision, `8ebf600ea834ec9c3cd274f9459d859ac4a33593`,
serving one passthrough route (`switchyard/passthrough`,
`OTEL_SDK_DISABLED=true`) after one non-streaming `POST /v1/responses`, routed
to a local synthetic OpenAI Responses upstream that answered with 120 input and
45 output tokens. No provider was contacted and no credential was configured.
The one upstream commit between the two revisions changes only
`crates/switchyard-translation`, the translation modules of three binding
crates and a translation doc (it removes an unused `UnknownFieldPolicy`), not
`crates/switchyard-server` or any metric; `--help` and `--version` from
the `46331725` image are byte-identical to the committed fixtures.

SHA-256 is calculated over the exact committed bytes, including the trailing
newline. A revision change requires rebuilding and reviewing both fixtures and
this record together; `tests/test_switchyard_probe.py` enforces the binding.

| Fixture | SHA-256 |
|---|---|
| `server-help.txt` | `a4c940a61111ece13d7c2380ac6dae0b1bc01d87a192caed3ad33a2d1725e820` |
| `server-version.txt` | `16e1b43d430a9c6f9690598c62a4f7d11d6e9b3cf8da04fa0d3d21b6482e8e53` |
