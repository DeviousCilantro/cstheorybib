from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--engine", choices=["auto", "rust", "python"], default=os.environ.get("THEORYBIB_ENGINE", "auto"))
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
    if engine in {"auto", "rust"} and cargo_available():
        cmd = rust_command(root, args, debug=debug)
        return subprocess.call(cmd, cwd=str(root))

    if engine == "rust":
        print("error: --engine rust requested, but cargo is not on PATH", file=sys.stderr)
        return 127

    py_args = list(args)
    if "--json-only" not in py_args:
        py_args.insert(0, "--json-only")
    cmd = [sys.executable, str(root / "gen_python.py"), *py_args]
    return subprocess.call(cmd, cwd=str(root))


def cargo_available() -> bool:
    return shutil.which("cargo") is not None


def rust_command(root: Path, args: list[str], *, debug: bool) -> list[str]:
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
    return base + (".exe" if os.name == "nt" else "")


if __name__ == "__main__":
    raise SystemExit(main())
