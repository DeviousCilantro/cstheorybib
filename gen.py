"""Wrapper script that dispatches to the Rust or Python bibliography generator.

Selects the implementation to use (auto/rust/python) via ``--engine``, then
delegates all remaining arguments to the chosen engine.  When ``--engine auto``
(the default) the Rust binary is preferred whenever ``cargo`` is on PATH.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    """Parse wrapper arguments and dispatch to the selected engine.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]`` when None.

    Returns:
        The exit code returned by the underlying engine process.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    root = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--engine",
        choices=["auto", "rust", "python"],
        default=os.environ.get("THEORYBIB_ENGINE", "auto"),
    )
    parser.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("-h", "--help", action="store_true")
    ns, rest = parser.parse_known_args(argv)

    if ns.help:
        print("theorybib generator wrapper")
        print()
        print("Wrapper options:")
        print("  --engine auto|rust|python   choose implementation; default: auto")
        print()
        print("Engine help follows. Most options are forwarded unchanged.")
        print()
        sys.stdout.flush()
        return run_engine(root, ns.engine, ["--help"], debug=ns.debug)

    return run_engine(root, ns.engine, rest, debug=ns.debug)


def run_engine(root: Path, engine: str, args: list[str], *, debug: bool = False) -> int:
    """Build the command for the requested engine and run it.

    Args:
        root: Repository root directory.
        engine: One of ``"auto"``, ``"rust"``, or ``"python"``.
        args: Arguments forwarded verbatim to the engine.
        debug: When True, prefer a debug build over a release build.

    Returns:
        The exit code of the subprocess.
    """
    if engine in {"auto", "rust"} and cargo_available():
        cmd = rust_command(root, args, debug=debug)
        return subprocess.call(cmd, cwd=str(root))

    if engine == "rust":
        print(
            "error: --engine rust requested, but cargo is not on PATH", file=sys.stderr
        )
        return 127

    py_args = list(args)
    if "--json-only" not in py_args:
        py_args.insert(0, "--json-only")
    cmd = [sys.executable, str(root / "gen_python.py"), *py_args]
    return subprocess.call(cmd, cwd=str(root))


def cargo_available() -> bool:
    """Return True if the ``cargo`` executable is on PATH."""
    return shutil.which("cargo") is not None


def rust_command(root: Path, args: list[str], *, debug: bool) -> list[str]:
    """Build the cargo/binary command for the Rust engine.

    Prefers an already-compiled binary when one exists; otherwise falls back to
    ``cargo run``.

    Args:
        root: Repository root directory.
        args: Arguments forwarded to the binary.
        debug: When True, target the debug profile instead of release.

    Returns:
        The command list suitable for ``subprocess.call``.
    """
    manifest = root / "rust" / "theorybib-dblp" / "Cargo.toml"
    release_bin = root / "target" / "release" / bin_name("theorybib-dblp")
    debug_bin = root / "target" / "debug" / bin_name("theorybib-dblp")

    if not debug and release_bin.exists():
        return [str(release_bin), *args]
    if debug and debug_bin.exists():
        return [str(debug_bin), *args]

    cmd = ["cargo", "run", "--quiet"]
    if not debug:
        cmd.append("--release")
    cmd.extend(["--manifest-path", str(manifest), "--"])
    cmd.extend(args)
    return cmd


def bin_name(base: str) -> str:
    """Return *base* with a ``.exe`` suffix on Windows, unchanged elsewhere.

    Args:
        base: The platform-independent binary name.

    Returns:
        The OS-appropriate binary filename.
    """
    return base + (".exe" if os.name == "nt" else "")


if __name__ == "__main__":
    raise SystemExit(main())
