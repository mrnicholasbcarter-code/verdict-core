"""Verify the Mermaid diagrams in diagrams/*.mmd stay honest about the code (P1-4).

Every diagram must exist, start with a valid Mermaid diagram type, have
balanced brackets/quotes outside its comment lines, and cite source files and
functions that actually exist in the repository. Every ```mermaid block
embedded in README.md must be byte-identical to one of the diagrams/*.mmd
files -- README embeds are not allowed to drift from the verified source.

Symbol citations are parsed only from the structured header lines
("%%   path/to/file.py (Symbol1, Symbol2, ...)"), not from the freeform prose
in "%% evidence: ..." lines, which describe behavior in plain English and are
not meant to be machine-parsed for identifiers.

If `mmdc` (the Mermaid CLI) is available and can run headless without a
network fetch, this also asks it to parse each diagram; when it is not
available (the common case in an offline CI runner), that check is skipped
rather than failing the whole test.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIAGRAMS_DIR = ROOT / "diagrams"
README = ROOT / "README.md"

EXPECTED_DIAGRAMS = (
    "ecosystem",
    "eligibility-ladder",
    "explain-flow",
    "orchestration-flow",
    "route-flow",
    "setup-detection-flow",
)

VALID_DIAGRAM_TYPES = (
    "flowchart",
    "graph",
    "sequenceDiagram",
    "stateDiagram-v2",
    "stateDiagram",
)

# file:line or file:line-line references inside a %% comment line, e.g.
# "verdict/api.py:944" or "verdict/orchestration/eligibility.py:358-362".
FILE_LINE_RE = re.compile(r"\b((?:verdict|verdict-core)/[\w./-]+\.py):(\d+)(?:-(\d+))?")

# Structured header lines only, of the shape:
#   %%   verdict/orchestration/eligibility.py (Symbol1, Symbol2, ...)
# This intentionally does NOT parse the freeform "%% evidence: ..." prose
# lines, which describe behavior and would produce false-positive "symbols"
# out of ordinary English words next to a file:line citation.
HEADER_SOURCE_LINE_RE = re.compile(
    r"^%%\s+((?:verdict|verdict-core)[\w./-]*\.py)\s*\(([^)]*)\)\s*$"
)


def _diagram_paths() -> list[Path]:
    return sorted(DIAGRAMS_DIR.glob("*.mmd"))


def _strip_comments(text: str) -> str:
    """Return only the non-%%-comment lines of a .mmd file."""
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("%%")
    )


def _first_code_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("%%"):
            return stripped
    return ""


def _check_balance(code: str) -> None:
    """Bracket/paren/brace/quote balance check over non-comment diagram code."""
    counts = {
        "(": code.count("(") - code.count(")"),
        "[": code.count("[") - code.count("]"),
        "{": code.count("{") - code.count("}"),
    }
    for opener, delta in counts.items():
        assert delta == 0, f"unbalanced {opener!r}: net difference {delta}"
    assert code.count('"') % 2 == 0, "odd number of double-quote characters"


def test_all_six_diagrams_exist() -> None:
    found = {p.stem for p in _diagram_paths()}
    missing = set(EXPECTED_DIAGRAMS) - found
    assert not missing, f"missing diagrams/*.mmd: {sorted(missing)}"


@pytest.mark.parametrize("name", EXPECTED_DIAGRAMS)
def test_diagram_starts_with_a_valid_type(name: str) -> None:
    path = DIAGRAMS_DIR / f"{name}.mmd"
    assert path.exists(), f"{path} does not exist"
    first_line = _first_code_line(path.read_text(encoding="utf-8"))
    assert any(first_line.startswith(t) for t in VALID_DIAGRAM_TYPES), (
        f"{path}: first non-comment line {first_line!r} is not a recognized "
        f"Mermaid diagram type ({VALID_DIAGRAM_TYPES})"
    )


@pytest.mark.parametrize("name", EXPECTED_DIAGRAMS)
def test_diagram_brackets_and_quotes_are_balanced(name: str) -> None:
    path = DIAGRAMS_DIR / f"{name}.mmd"
    code = _strip_comments(path.read_text(encoding="utf-8"))
    _check_balance(code)


def test_readme_mermaid_blocks_are_byte_identical_to_a_diagram_file() -> None:
    """Every ```mermaid fenced block in README.md must match one diagrams/*.mmd file exactly."""
    readme_text = README.read_text(encoding="utf-8")
    blocks = re.findall(r"```mermaid\n(.*?)```", readme_text, flags=re.DOTALL)
    assert blocks, "README.md has no ```mermaid blocks; expected at least 3 embedded diagrams"

    diagram_contents = {p.name: p.read_text(encoding="utf-8") for p in _diagram_paths()}
    unmatched: list[str] = []
    for block in blocks:
        if block not in diagram_contents.values():
            unmatched.append(block[:80])
    assert not unmatched, (
        "README.md mermaid block(s) do not byte-match any diagrams/*.mmd file "
        f"(first 80 chars shown): {unmatched}"
    )


def test_readme_embeds_at_least_three_diagrams() -> None:
    readme_text = README.read_text(encoding="utf-8")
    blocks = re.findall(r"```mermaid\n(.*?)```", readme_text, flags=re.DOTALL)
    diagram_contents = {p.name: p.read_text(encoding="utf-8") for p in _diagram_paths()}
    embedded_names = {name for name, content in diagram_contents.items() if content in blocks}
    required = {"route-flow.mmd", "eligibility-ladder.mmd", "orchestration-flow.mmd"}
    missing = required - embedded_names
    assert not missing, f"README.md is missing embedded diagram(s): {sorted(missing)}"


def _cited_file_line_refs(text: str) -> list[tuple[str, int, int | None]]:
    refs: list[tuple[str, int, int | None]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("%%"):
            continue
        for match in FILE_LINE_RE.finditer(stripped):
            rel_path, start, end = match.group(1), int(match.group(2)), match.group(3)
            refs.append((rel_path, start, int(end) if end else None))
    return refs


def _cited_symbols_from_header(text: str) -> list[tuple[str, str]]:
    """(file, symbol) pairs parsed ONLY from the structured header lines:
    "%%   path/to/file.py (Symbol1, Symbol2, ...)". Freeform "%% evidence: ..."
    prose lines are intentionally not parsed here."""
    pairs: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = HEADER_SOURCE_LINE_RE.match(line.strip())
        if not match:
            continue
        rel_path, symbol_list = match.group(1), match.group(2)
        for raw in symbol_list.split(","):
            symbol = raw.strip()
            # Header lists may include dotted paths like "EligibilityLadder._assess";
            # the leaf identifier after the last dot is what we grep for.
            symbol = symbol.rsplit(".", 1)[-1]
            # Skip parenthetical asides and non-identifier tokens (e.g. "not wired").
            if symbol and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", symbol):
                pairs.append((rel_path, symbol))
    return pairs


# ecosystem.mmd cites package.json/pyproject.toml files across several repos
# (not all inside this checkout, and not Python def/class symbols), so it is
# checked separately below rather than against the Python-symbol-shaped rules.
PYTHON_SYMBOL_DIAGRAMS = tuple(name for name in EXPECTED_DIAGRAMS if name != "ecosystem")


@pytest.mark.parametrize("name", PYTHON_SYMBOL_DIAGRAMS)
def test_every_cited_source_file_exists(name: str) -> None:
    path = DIAGRAMS_DIR / f"{name}.mmd"
    text = path.read_text(encoding="utf-8")
    refs = _cited_file_line_refs(text)
    header_pairs = _cited_symbols_from_header(text)
    assert refs or header_pairs, f"{path} has no source citations (file:line or header) to check"
    all_paths = {rel_path for rel_path, _, _ in refs} | {rel_path for rel_path, _ in header_pairs}
    missing = sorted({rel_path for rel_path in all_paths if not (ROOT / rel_path).exists()})
    assert not missing, f"{path} cites source file(s) that do not exist: {missing}"


def test_ecosystem_cited_in_repo_files_exist() -> None:
    """ecosystem.mmd cites pyproject.toml/package.json paths in THIS repo plus
    absolute paths into sibling checkouts on the local machine. Only the
    in-repo relative paths are checked for existence here; sibling-repo
    absolute paths are outside this repo's control and are not asserted."""
    path = DIAGRAMS_DIR / "ecosystem.mmd"
    text = path.read_text(encoding="utf-8")
    in_repo_candidates = re.findall(r"\b(pyproject\.toml|contracts/package\.json)\b", text)
    assert in_repo_candidates, f"{path} cites no in-repo file for a sanity check"
    missing = sorted({p for p in in_repo_candidates if not (ROOT / p).exists()})
    assert not missing, f"{path} cites in-repo file(s) that do not exist: {missing}"


@pytest.mark.parametrize("name", EXPECTED_DIAGRAMS)
def test_every_cited_line_is_within_file_bounds(name: str) -> None:
    path = DIAGRAMS_DIR / f"{name}.mmd"
    text = path.read_text(encoding="utf-8")
    refs = _cited_file_line_refs(text)
    problems: list[str] = []
    for rel_path, start, end in refs:
        target = ROOT / rel_path
        if not target.exists():
            continue  # covered by test_every_cited_source_file_exists
        line_count = len(target.read_text(encoding="utf-8").splitlines())
        last = end or start
        if last > line_count:
            problems.append(
                f"{rel_path}:{start}" + (f"-{end}" if end else "") + f" (file has {line_count} lines)"
            )
    assert not problems, f"{path} cites out-of-range line number(s): {problems}"


@pytest.mark.parametrize("name", PYTHON_SYMBOL_DIAGRAMS)
def test_every_header_symbol_exists_in_its_file(name: str) -> None:
    """Grep-based check: every symbol named in a diagram's structured header
    line ("%%   path.py (Symbol1, Symbol2, ...)") must appear as a def/class
    somewhere in that file (not necessarily on a cited line -- files shift)."""
    path = DIAGRAMS_DIR / f"{name}.mmd"
    text = path.read_text(encoding="utf-8")
    pairs = _cited_symbols_from_header(text)
    assert pairs, f"{path} header has no (Symbol, ...) list to check"
    missing: list[str] = []
    seen: set[tuple[str, str]] = set()
    for rel_path, symbol in pairs:
        if (rel_path, symbol) in seen:
            continue
        seen.add((rel_path, symbol))
        target = ROOT / rel_path
        if not target.exists():
            continue  # covered by test_every_cited_source_file_exists
        content = target.read_text(encoding="utf-8")
        pattern = re.compile(
            rf"^\s*(?:async\s+def|def|class)\s+{re.escape(symbol)}\b", re.MULTILINE
        )
        if not pattern.search(content):
            missing.append(f"{rel_path}: {symbol}")
    assert not missing, f"{path} header cites symbol(s) not found via def/class grep: {missing}"


def _mmdc_available() -> str | None:
    """Return a usable mmdc executable path, or None if not present."""
    return shutil.which("mmdc")


@pytest.mark.skipif(_mmdc_available() is None, reason="mmdc (mermaid-cli) is not installed")
@pytest.mark.parametrize("name", EXPECTED_DIAGRAMS)
def test_mmdc_can_parse_diagram_when_available(name: str, tmp_path: Path) -> None:
    """Best-effort: if mmdc works headlessly in this environment, use it to
    validate syntax. Any failure that looks like a missing headless-Chrome
    dependency (common in sandboxes without a pre-fetched browser) is treated
    as "not actually available here" rather than a diagram defect."""
    mmdc = _mmdc_available()
    assert mmdc is not None
    src = DIAGRAMS_DIR / f"{name}.mmd"
    out = tmp_path / f"{name}.svg"
    result = subprocess.run(
        [mmdc, "-i", str(src), "-o", str(out)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0 and (
        "chrome-headless-shell" in result.stderr or "Could not find" in result.stderr
    ):
        pytest.skip("mmdc present but headless Chrome is not installed in this environment")
    assert result.returncode == 0, f"mmdc failed to parse {src}:\n{result.stderr}"
