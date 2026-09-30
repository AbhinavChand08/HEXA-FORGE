"""Layering rules of design section 4.1."""
import ast
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
FORBIDDEN = ("app.registry", "app.db", "app.mapping", "app.compliance", "psycopg", "ml")


def _imports(path: Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_parsing_and_templating_never_import_the_registry():
    for pkg in ("parsing", "templating"):
        for path in (APP / pkg).rglob("*.py"):
            bad = {m for m in _imports(path) if m.split(".")[0] in FORBIDDEN
                   or m.startswith(FORBIDDEN)}
            assert not bad, f"{path.relative_to(APP)} imports {sorted(bad)}"


def test_normalizer_does_not_load_the_ml_stack():
    code = ("import sys, app.mapping.normalizer; "
            "print(sorted(m for m in sys.modules if m.split('.')[0] in "
            "('ml', 'sentence_transformers', 'sklearn')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=APP.parent, check=True).stdout.strip()
    assert out == "[]"
