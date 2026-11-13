"""Repo map generator — structural "table of contents" for a codebase.

Extracts top-level symbols (functions, classes, interfaces, structs) from
source files and produces a concise map that fits within a token budget.
Agents use this to navigate the repo without blindly reading files.

Uses Python's ast module for .py files and regex patterns for other
languages. Zero external dependencies.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

# Directories to always skip
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "node_modules",
        ".venv",
        "venv",
        ".env",
        "env",
        ".tox",
        "dist",
        "build",
        ".eggs",
        ".next",
        ".nuxt",
        "target",  # Rust/Java build output
        "vendor",  # Go vendor
        ".worktrees",
        "coverage",
        ".coverage",
        "htmlcov",
    }
)

# File extensions we know how to parse
_SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".java",
        ".rb",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cc",
        ".cs",
    }
)


@dataclass
class SymbolDef:
    """A symbol definition extracted from source code."""

    name: str
    kind: str  # function, class, method, struct, interface, trait, etc.
    line: int = 0
    signature: str = ""
    children: list[SymbolDef] = field(default_factory=list)


def extract_symbols(path: Path) -> list[SymbolDef]:
    """Extract top-level symbol definitions from a source file.

    Returns an empty list for unsupported files, binary files, or parse errors.
    """
    if not path.is_file():
        return []

    suffix = path.suffix.lower()
    if suffix not in _SUPPORTED_EXTENSIONS:
        return []

    try:
        source = path.read_text(encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, OSError):
        return []

    # Quick binary check
    if "\x00" in source:
        return []

    if suffix == ".py":
        return _extract_python(source)
    if suffix in {".js", ".jsx", ".ts", ".tsx"}:
        return _extract_js_ts(source)
    if suffix == ".go":
        return _extract_go(source)
    if suffix == ".rs":
        return _extract_rust(source)
    if suffix == ".java":
        return _extract_java(source)
    if suffix == ".rb":
        return _extract_ruby(source)
    if suffix in {".c", ".h", ".cpp", ".hpp", ".cc"}:
        return _extract_c_cpp(source)
    if suffix == ".cs":
        return _extract_csharp(source)

    return []


# ---------------------------------------------------------------------------
# Python — uses ast for accuracy
# ---------------------------------------------------------------------------


def _extract_python(source: str) -> list[SymbolDef]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    symbols: list[SymbolDef] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            sig = _py_func_signature(node)
            symbols.append(
                SymbolDef(
                    name=node.name, kind="function", line=node.lineno, signature=sig
                )
            )
        elif isinstance(node, ast.ClassDef):
            children: list[SymbolDef] = []
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    csig = _py_func_signature(child)
                    children.append(
                        SymbolDef(
                            name=child.name,
                            kind="method",
                            line=child.lineno,
                            signature=csig,
                        )
                    )
            bases = ", ".join(ast.unparse(b) for b in node.bases)
            sig = f"class {node.name}({bases})" if bases else f"class {node.name}"
            symbols.append(
                SymbolDef(
                    name=node.name,
                    kind="class",
                    line=node.lineno,
                    signature=sig,
                    children=children,
                )
            )
    return symbols


def _py_func_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Build a concise function signature string."""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    args = ast.unparse(node.args)
    ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"{prefix} {node.name}({args}){ret}"


# ---------------------------------------------------------------------------
# JavaScript / TypeScript — regex-based
# ---------------------------------------------------------------------------

_JS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)",
            re.MULTILINE,
        ),
        "function",
    ),
    (
        re.compile(r"^(?:export\s+)?class\s+(\w+)", re.MULTILINE),
        "class",
    ),
    (
        re.compile(r"^(?:export\s+)?interface\s+(\w+)", re.MULTILINE),
        "interface",
    ),
    (
        re.compile(r"^(?:export\s+)?type\s+(\w+)", re.MULTILINE),
        "type",
    ),
    (
        re.compile(r"^(?:export\s+)?enum\s+(\w+)", re.MULTILINE),
        "enum",
    ),
]


def _extract_js_ts(source: str) -> list[SymbolDef]:
    return _regex_extract(source, _JS_PATTERNS)


# ---------------------------------------------------------------------------
# Go — regex-based
# ---------------------------------------------------------------------------

_GO_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"^type\s+(\w+)\s+struct\b", re.MULTILINE),
        "struct",
    ),
    (
        re.compile(r"^type\s+(\w+)\s+interface\b", re.MULTILINE),
        "interface",
    ),
    (
        re.compile(r"^func\s+(\w+)\s*\(", re.MULTILINE),
        "function",
    ),
    (
        re.compile(r"^func\s+\([^)]+\)\s+(\w+)\s*\(", re.MULTILINE),
        "method",
    ),
]


def _extract_go(source: str) -> list[SymbolDef]:
    return _regex_extract(source, _GO_PATTERNS)


# ---------------------------------------------------------------------------
# Rust — regex-based
# ---------------------------------------------------------------------------

_RUST_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"^(?:pub\s+)?struct\s+(\w+)", re.MULTILINE),
        "struct",
    ),
    (
        re.compile(r"^(?:pub\s+)?enum\s+(\w+)", re.MULTILINE),
        "enum",
    ),
    (
        re.compile(r"^(?:pub\s+)?trait\s+(\w+)", re.MULTILINE),
        "trait",
    ),
    (
        re.compile(
            r"^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)",
            re.MULTILINE,
        ),
        "function",
    ),
]


def _extract_rust(source: str) -> list[SymbolDef]:
    return _regex_extract(source, _RUST_PATTERNS)


# ---------------------------------------------------------------------------
# Java — regex-based
# ---------------------------------------------------------------------------

_JAVA_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"^(?:public|private|protected)?\s*(?:static\s+)?class\s+(\w+)",
            re.MULTILINE,
        ),
        "class",
    ),
    (
        re.compile(
            r"^(?:public|private|protected)?\s*interface\s+(\w+)",
            re.MULTILINE,
        ),
        "interface",
    ),
    (
        re.compile(r"^(?:public|private|protected)?\s*enum\s+(\w+)", re.MULTILINE),
        "enum",
    ),
]


def _extract_java(source: str) -> list[SymbolDef]:
    return _regex_extract(source, _JAVA_PATTERNS)


# ---------------------------------------------------------------------------
# Ruby — regex-based
# ---------------------------------------------------------------------------

_RUBY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\s*class\s+(\w+)", re.MULTILINE), "class"),
    (re.compile(r"^\s*module\s+(\w+)", re.MULTILINE), "module"),
    (re.compile(r"^\s*def\s+(\w+)", re.MULTILINE), "function"),
]


def _extract_ruby(source: str) -> list[SymbolDef]:
    return _regex_extract(source, _RUBY_PATTERNS)


# ---------------------------------------------------------------------------
# C / C++ — regex-based
# ---------------------------------------------------------------------------

_C_CPP_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"^(?:class|struct)\s+(\w+)", re.MULTILINE),
        "class",
    ),
    (
        re.compile(
            r"^(?:[\w:*&<>]+\s+)+(\w+)\s*\([^;]*$",
            re.MULTILINE,
        ),
        "function",
    ),
]


def _extract_c_cpp(source: str) -> list[SymbolDef]:
    return _regex_extract(source, _C_CPP_PATTERNS)


# ---------------------------------------------------------------------------
# C# — regex-based
# ---------------------------------------------------------------------------

_CSHARP_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"^(?:public|private|protected|internal)?\s*(?:static\s+)?class\s+(\w+)",
            re.MULTILINE,
        ),
        "class",
    ),
    (
        re.compile(
            r"^(?:public|private|protected|internal)?\s*interface\s+(\w+)",
            re.MULTILINE,
        ),
        "interface",
    ),
]


def _extract_csharp(source: str) -> list[SymbolDef]:
    return _regex_extract(source, _CSHARP_PATTERNS)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _regex_extract(
    source: str,
    patterns: list[tuple[re.Pattern[str], str]],
) -> list[SymbolDef]:
    """Extract symbols using a list of (pattern, kind) pairs."""
    seen: set[tuple[str, str]] = set()
    symbols: list[SymbolDef] = []
    lines = source.split("\n")

    for pattern, kind in patterns:
        for match in pattern.finditer(source):
            name = match.group(1)
            key = (name, kind)
            if key in seen:
                continue
            seen.add(key)
            # Calculate line number
            line_num = source[: match.start()].count("\n") + 1
            # Get the matched line as signature
            line_idx = line_num - 1
            sig = lines[line_idx].strip() if line_idx < len(lines) else ""
            symbols.append(
                SymbolDef(name=name, kind=kind, line=line_num, signature=sig)
            )

    symbols.sort(key=lambda s: s.line)
    return symbols


def _should_skip_dir(name: str) -> bool:
    """Check whether a directory name should be skipped."""
    if name in _SKIP_DIRS:
        return True
    if name.startswith("."):
        return True
    return bool(name.endswith(".egg-info"))


def _collect_source_files(root: Path) -> list[Path]:
    """Walk the tree and collect supported source files, skipping junk dirs."""
    files: list[Path] = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if not _should_skip_dir(entry.name):
                    stack.append(entry)
            elif entry.is_file() and entry.suffix.lower() in _SUPPORTED_EXTENSIONS:
                files.append(entry)
    return sorted(files)


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 characters per token."""
    return max(1, len(text) // 4)


def generate_repo_map(
    root: Path,
    *,
    max_tokens: int = 1024,
    hint: str = "",
) -> str:
    """Generate a concise repo map for the given directory.

    Args:
        root: Repository root directory.
        max_tokens: Approximate token budget for the output.
        hint: Optional text (e.g. issue title/body) used to prioritize
              files whose names appear in the hint.

    Returns:
        A string containing the repo map, or "" if no symbols found.
    """
    source_files = _collect_source_files(root)
    if not source_files:
        return ""

    # Extract symbols per file
    file_symbols: list[tuple[Path, list[SymbolDef]]] = []
    for fpath in source_files:
        syms = extract_symbols(fpath)
        if syms:
            file_symbols.append((fpath, syms))

    if not file_symbols:
        return ""

    # Score files: prioritize those mentioned in the hint
    hint_lower = hint.lower()

    def _file_score(item: tuple[Path, list[SymbolDef]]) -> tuple[int, str]:
        fpath, syms = item
        rel = str(fpath.relative_to(root))
        # Higher score (lower sort key) if file name or symbols match hint
        hint_match = 0
        if hint_lower:
            rel_lower = rel.lower()
            for word in hint_lower.split():
                if len(word) > 2 and word in rel_lower:
                    hint_match -= 10
                for sym in syms:
                    if len(word) > 2 and word in sym.name.lower():
                        hint_match -= 5
        # Secondary: more symbols = more important
        return (hint_match, rel)

    file_symbols.sort(key=_file_score)

    # Build output, respecting token budget
    parts: list[str] = []
    tokens_used = 0
    files_included = 0

    for fpath, syms in file_symbols:
        rel = str(fpath.relative_to(root))

        # Try full section first
        section = _format_file_section(rel, syms)
        section_tokens = _estimate_tokens(section)

        if tokens_used + section_tokens <= max_tokens:
            parts.append(section)
            tokens_used += section_tokens
            files_included += 1
            continue

        # Budget exceeded — try truncating symbols within this file
        if not parts:
            # First file: include as many symbols as fit
            truncated = _format_file_section_truncated(
                rel, syms, max_tokens - tokens_used
            )
            if truncated:
                parts.append(truncated)
                tokens_used += _estimate_tokens(truncated)
                files_included += 1
            break
        else:
            # Already have some files — stop here
            remaining = len(file_symbols) - files_included
            if remaining > 0:
                parts.append(f"... and {remaining} more files")
            break

    return "\n".join(parts)


def _format_file_section_truncated(
    rel_path: str, symbols: list[SymbolDef], max_tokens: int
) -> str:
    """Format a file section, truncating symbols to fit token budget."""
    lines = [rel_path]
    for sym in symbols:
        prefix = "│"
        line = (
            f"  {prefix} {sym.signature}"
            if sym.signature
            else f"  {prefix} {sym.kind} {sym.name}"
        )
        candidate = "\n".join(lines + [line])
        if _estimate_tokens(candidate) > max_tokens and len(lines) > 1:
            remaining = len(symbols) - (len(lines) - 1)
            lines.append(f"  │ ... and {remaining} more symbols")
            break
        lines.append(line)
        # Skip children in truncated mode to save space
    return "\n".join(lines) if len(lines) > 1 else ""


def _format_file_section(rel_path: str, symbols: list[SymbolDef]) -> str:
    """Format a single file's symbols as a compact listing."""
    lines = [rel_path]
    for sym in symbols:
        prefix = "│"
        if sym.signature:
            lines.append(f"  {prefix} {sym.signature}")
        else:
            lines.append(f"  {prefix} {sym.kind} {sym.name}")
        for child in sym.children:
            if child.signature:
                lines.append(f"  {prefix}   {child.signature}")
            else:
                lines.append(f"  {prefix}   {child.kind} {child.name}")
    return "\n".join(lines)
