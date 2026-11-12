# Switchyard fixture provenance

The `server-help.txt` and `server-version.txt` fixtures are the public stdout of
`switchyard-server --help` and `switchyard-server --version`, built from a clean
checkout of upstream revision `6ff491acd7eb1ec239525a1baa015730c3d3564a` with
the repository lockfile and the upstream-pinned Rust 1.96.1 toolchain:

```text
cargo +1.96.1 build --locked --release --package switchyard-server
target/release/switchyard-server --help
target/release/switchyard-server --version
```

No provider credentials were configured, no provider request was made, and no
proxy was launched.

SHA-256 is calculated over the exact committed bytes, including the trailing
newline. A revision change requires rebuilding and reviewing both fixtures and
this record together; `tests/test_switchyard_probe.py` enforces the binding.

| Fixture | SHA-256 |
|---|---|
| `server-help.txt` | `6c29d81458b24a523d05dcb6d199dec00fd0e0ab0d66bd214ff27bdc442097e8` |
| `server-version.txt` | `4fd5a5854be50625998518c7f5a491d68cc4b7327960fe0a16b0a19ec8a88e6b` |
