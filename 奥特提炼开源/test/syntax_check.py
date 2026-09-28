"""AST-parse all Python files in app/ to ensure no syntax regression."""
from __future__ import annotations

import ast
import pathlib
import sys


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    errors: list[str] = []
    for path in sorted(root.rglob("*.py")):
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            errors.append(f"{path}: {exc}")
    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        return 1
    print(f"OK: parsed {sum(1 for _ in root.rglob('*.py'))} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
