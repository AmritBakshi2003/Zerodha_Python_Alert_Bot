#!/usr/bin/env python3
"""Fail if any internal ``kite_alerts.*`` import names a module that does not exist.

Why this exists: ``src/kite_alerts/data/`` was once matched by an unanchored
``data/`` rule in ``.gitignore``, so the package was never committed. Every file
that imported it still parsed and type-checked, yet nothing could be imported or
tested from a clean clone. This check closes that gap by resolving each internal
import against the actual module tree.

Imports are parsed statically rather than imported. Importing would execute side
effects (environment reads, network clients) and a missing module would surface
as one ``ImportError`` from whichever module was probed first, instead of a
named per-file, per-line failure.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
TESTS = ROOT / "tests"
PACKAGE = "kite_alerts"


def module_parts(path: Path, base: Path) -> list[str]:
    """Dotted module parts for ``path`` relative to the import root ``base``."""
    parts = list(path.relative_to(base).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return parts


def package_parts(path: Path, base: Path) -> list[str]:
    """Parts that a *relative* import inside ``path`` is resolved against."""
    parts = list(path.relative_to(base).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
        # An ``__init__`` resolves its own relative imports against its directory.
        return parts
    # A regular module resolves them against its containing package.
    return parts[:-1]


def existing_modules() -> set[str]:
    """Every importable module name under ``src/`` (packages included)."""
    modules: set[str] = set()
    for path in SRC.rglob("*.py"):
        parts = module_parts(path, SRC)
        if parts:
            modules.add(".".join(parts))
    return modules


def is_internal(name: str) -> bool:
    return name == PACKAGE or name.startswith(PACKAGE + ".")


def resolve(level: int, module: str | None, package: list[str]) -> str | None:
    """Absolute dotted path for an import, or ``None`` if the level over-reaches."""
    if level == 0:
        return module
    trim = level - 1
    if trim > len(package):
        return None
    base = package[: len(package) - trim]
    tail = module.split(".") if module else []
    dotted = ".".join([*base, *tail])
    return dotted or None


def problems_in(path: Path, base: Path, modules: set[str]) -> list[tuple[int, str, str]]:
    """Broken internal imports in one file: ``(line, module, reason)``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = package_parts(path, base)
    found: list[tuple[int, str, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if is_internal(alias.name) and alias.name not in modules:
                    found.append((node.lineno, alias.name, "module does not exist"))
        elif isinstance(node, ast.ImportFrom):
            target = resolve(node.level, node.module, package)
            if target is None or not is_internal(target):
                continue
            if target in modules:
                continue
            # ``from kite_alerts.x import y`` may name a submodule of the target.
            if any(f"{target}.{alias.name}" in modules for alias in node.names):
                continue
            found.append((node.lineno, target, "module does not exist"))

    return found


def main() -> int:
    if not SRC.is_dir():
        print(f"error: no source root at {SRC}", file=sys.stderr)
        return 1

    modules = existing_modules()
    if not modules:
        # A check that scans nothing must never report success.
        print(f"error: no modules discovered under {SRC}", file=sys.stderr)
        return 1

    files = sorted([*SRC.rglob("*.py"), *TESTS.rglob("*.py")])
    if not files:
        print("error: no Python files to scan", file=sys.stderr)
        return 1

    failures = 0
    for path in files:
        base = SRC if path.is_relative_to(SRC) else TESTS
        for lineno, name, reason in problems_in(path, base, modules):
            print(f"{path.relative_to(ROOT)}:{lineno}: {name}: {reason}")
            failures += 1

    if failures:
        print(f"\nFAILED: {failures} broken internal import(s) across {len(files)} file(s)")
        return 1

    print(f"OK: {len(modules)} modules, {len(files)} files scanned, all internal imports resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
