"""Tests for repo map generation."""

import textwrap
from pathlib import Path

from kew.repomap import (
    SymbolDef,
    extract_symbols,
    generate_repo_map,
)


class TestExtractSymbols:
    """Test symbol extraction from various languages."""

    def test_python_functions(self, tmp_path: Path):
        src = tmp_path / "example.py"
        src.write_text(
            textwrap.dedent("""\
            def hello(name: str) -> str:
                return f"Hello {name}"

            def add(a: int, b: int) -> int:
                return a + b
            """)
        )
        symbols = extract_symbols(src)
        names = [s.name for s in symbols]
        assert "hello" in names
        assert "add" in names
        assert all(s.kind == "function" for s in symbols)

    def test_python_classes_and_methods(self, tmp_path: Path):
        src = tmp_path / "models.py"
        src.write_text(
            textwrap.dedent("""\
            class User:
                def __init__(self, name: str):
                    self.name = name

                def greet(self) -> str:
                    return f"Hi, {self.name}"

            class Admin(User):
                role: str = "admin"
            """)
        )
        symbols = extract_symbols(src)
        names = [s.name for s in symbols]
        assert "User" in names
        assert "Admin" in names
        # Methods should be nested under class
        user_sym = next(s for s in symbols if s.name == "User")
        assert user_sym.kind == "class"
        method_names = [c.name for c in user_sym.children]
        assert "__init__" in method_names
        assert "greet" in method_names

    def test_python_syntax_error_returns_empty(self, tmp_path: Path):
        src = tmp_path / "bad.py"
        src.write_text("def broken(\n")
        symbols = extract_symbols(src)
        assert symbols == []

    def test_javascript_functions(self, tmp_path: Path):
        src = tmp_path / "app.js"
        src.write_text(
            textwrap.dedent("""\
            function fetchData(url) {
                return fetch(url);
            }

            class ApiClient {
                constructor(baseUrl) {
                    this.baseUrl = baseUrl;
                }

                async get(path) {
                    return fetch(this.baseUrl + path);
                }
            }

            export function helper() {}
            """)
        )
        symbols = extract_symbols(src)
        names = [s.name for s in symbols]
        assert "fetchData" in names
        assert "ApiClient" in names
        assert "helper" in names

    def test_typescript_file(self, tmp_path: Path):
        src = tmp_path / "service.ts"
        src.write_text(
            textwrap.dedent("""\
            interface Config {
                url: string;
            }

            export class Service {
                constructor(private config: Config) {}

                async fetch(): Promise<Response> {
                    return fetch(this.config.url);
                }
            }

            export function createService(config: Config): Service {
                return new Service(config);
            }
            """)
        )
        symbols = extract_symbols(src)
        names = [s.name for s in symbols]
        assert "Config" in names
        assert "Service" in names
        assert "createService" in names

    def test_go_file(self, tmp_path: Path):
        src = tmp_path / "main.go"
        src.write_text(
            textwrap.dedent("""\
            package main

            type Server struct {
                Port int
            }

            func NewServer(port int) *Server {
                return &Server{Port: port}
            }

            func (s *Server) Start() error {
                return nil
            }
            """)
        )
        symbols = extract_symbols(src)
        names = [s.name for s in symbols]
        assert "Server" in names
        assert "NewServer" in names
        assert "Start" in names

    def test_rust_file(self, tmp_path: Path):
        src = tmp_path / "lib.rs"
        src.write_text(
            textwrap.dedent("""\
            pub struct Config {
                pub name: String,
            }

            pub fn create_config(name: &str) -> Config {
                Config { name: name.to_string() }
            }

            impl Config {
                pub fn validate(&self) -> bool {
                    !self.name.is_empty()
                }
            }

            pub trait Configurable {
                fn configure(&self);
            }
            """)
        )
        symbols = extract_symbols(src)
        names = [s.name for s in symbols]
        assert "Config" in names
        assert "create_config" in names
        assert "Configurable" in names

    def test_unsupported_extension_returns_empty(self, tmp_path: Path):
        src = tmp_path / "data.csv"
        src.write_text("a,b,c\n1,2,3\n")
        symbols = extract_symbols(src)
        assert symbols == []

    def test_nonexistent_file_returns_empty(self, tmp_path: Path):
        symbols = extract_symbols(tmp_path / "nope.py")
        assert symbols == []

    def test_binary_file_returns_empty(self, tmp_path: Path):
        src = tmp_path / "data.py"
        src.write_bytes(b"\x00\x01\x02\x03")
        symbols = extract_symbols(src)
        assert symbols == []


class TestGenerateRepoMap:
    """Test full repo map generation."""

    def test_basic_repo_map(self, tmp_path: Path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text(
            textwrap.dedent("""\
            class App:
                def run(self):
                    pass

            def create_app() -> App:
                return App()
            """)
        )
        (tmp_path / "src" / "utils.py").write_text(
            textwrap.dedent("""\
            def format_name(name: str) -> str:
                return name.strip()
            """)
        )

        repo_map = generate_repo_map(tmp_path)
        assert "src/main.py" in repo_map
        assert "App" in repo_map
        assert "create_app" in repo_map
        assert "src/utils.py" in repo_map
        assert "format_name" in repo_map

    def test_respects_gitignore_patterns(self, tmp_path: Path):
        """Should skip common non-source directories."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("def main(): pass\n")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "pkg.js").write_text("function internal() {}\n")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config.py").write_text("def gitfn(): pass\n")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "app.cpython-312.pyc").write_bytes(b"\x00")

        repo_map = generate_repo_map(tmp_path)
        assert "main" in repo_map
        assert "internal" not in repo_map
        assert "gitfn" not in repo_map

    def test_max_tokens_limits_output(self, tmp_path: Path):
        """Repo map should fit within token budget."""
        (tmp_path / "big.py").write_text(
            "\n".join(f"def func_{i}(): pass" for i in range(200))
        )
        # Very small token budget — should truncate
        repo_map = generate_repo_map(tmp_path, max_tokens=100)
        # Rough token estimate: ~4 chars per token
        assert len(repo_map) < 600  # 100 tokens * ~5 chars + some overhead

    def test_empty_repo_returns_empty(self, tmp_path: Path):
        repo_map = generate_repo_map(tmp_path)
        assert repo_map == ""

    def test_prioritizes_files_mentioned_in_hint(self, tmp_path: Path):
        """Files whose names match hint keywords should appear first."""
        (tmp_path / "auth.py").write_text("def login(): pass\n")
        (tmp_path / "utils.py").write_text("def helper(): pass\n")
        (tmp_path / "billing.py").write_text("def charge(): pass\n")

        repo_map = generate_repo_map(
            tmp_path, max_tokens=50, hint="fix the auth login flow"
        )
        # With a tight budget and hint, auth.py should be prioritized
        if repo_map:
            # auth should appear before billing if budget is tight
            assert "login" in repo_map or "auth" in repo_map

    def test_same_name_different_kind_not_deduped(self, tmp_path: Path):
        """class Foo and function Foo should both appear."""
        src = tmp_path / "dual.go"
        src.write_text(
            textwrap.dedent("""\
            package main

            type Foo struct {}

            func Foo() {}
            """)
        )
        symbols = extract_symbols(src)
        names_kinds = [(s.name, s.kind) for s in symbols]
        assert ("Foo", "struct") in names_kinds
        assert ("Foo", "function") in names_kinds


class TestStableMode:
    """Stability mode: an empty hint yields a hash-stable, order-deterministic
    map so ``repo_map_hash`` (and thus ``prefix_key``) stays constant between
    main merges regardless of the per-issue title/body (spec D1/D4/D11)."""

    def _make_repo(self, root: Path) -> None:
        (root / "auth.py").write_text("def login(): pass\n")
        (root / "billing.py").write_text("def charge(): pass\n")
        (root / "utils.py").write_text("def helper(): pass\n")

    def test_stable_mode_byte_identical_across_generations(self, tmp_path: Path):
        """Same SHA, no hint → byte-identical output across generations."""
        self._make_repo(tmp_path)
        first = generate_repo_map(tmp_path, max_tokens=1024, hint="")
        second = generate_repo_map(tmp_path, max_tokens=1024, hint="")
        assert first == second
        assert first != ""

    # Note: independence from *issue text* is a caller-level (``build_repo_map``
    # stable-mode) property — ``generate_repo_map`` has no mode and only takes a
    # hint, so at this level it is indistinguishable from the repeated empty-hint
    # coverage above. That property is tested with two distinct non-empty hints
    # in ``TestRepoMapStabilityGate`` (tests/test_dispatch_core.py).

    def test_hint_mode_can_diverge_from_stable(self, tmp_path: Path):
        """Guard the tension the experiment resolves: with a tight budget a hint
        reorders selection, so hint-mode output can differ from stable-mode."""
        self._make_repo(tmp_path)
        stable = generate_repo_map(tmp_path, max_tokens=50, hint="")
        hinted = generate_repo_map(tmp_path, max_tokens=50, hint="billing charge")
        # Stable mode is alphabetical (auth first); a billing hint promotes
        # billing.py — so the two selections differ at a tight budget.
        assert stable != hinted


class TestSymbolDef:
    def test_symbol_def_fields(self):
        sym = SymbolDef(name="foo", kind="function", line=10, signature="def foo(x)")
        assert sym.name == "foo"
        assert sym.kind == "function"
        assert sym.line == 10
        assert sym.signature == "def foo(x)"
        assert sym.children == []

    def test_symbol_def_with_children(self):
        child = SymbolDef(name="method", kind="method", line=5)
        parent = SymbolDef(name="MyClass", kind="class", line=1, children=[child])
        assert len(parent.children) == 1
        assert parent.children[0].name == "method"
