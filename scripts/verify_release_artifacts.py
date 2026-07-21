#!/usr/bin/env python3
"""Build and install release artifacts in isolated environments.

This script is intentionally stdlib-only so CI can run it immediately after
`uv build`. It rejects missing, duplicate, or unimportable wheel/sdist outputs.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


def run(command: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(str(part) for part in command))
    subprocess.run(command, cwd=cwd, check=True)


def artifact_pair(dist: Path) -> tuple[Path, Path]:
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit(
            f"expected exactly one wheel and one sdist in {dist}; "
            f"found {len(wheels)} wheel(s) and {len(sdists)} sdist(s)"
        )
    return wheels[0], sdists[0]


def install_and_smoke_test(artifact: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="verdict-release-") as directory:
        environment = Path(directory) / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        run([str(python), "-m", "pip", "install", "--disable-pip-version-check", str(artifact)])
        run(
            [
                str(python),
                "-c",
                "import verdict; from verdict import Gate; print(verdict.__version__, Gate.__name__)",
            ]
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    args = parser.parse_args()
    wheel, sdist = artifact_pair(args.dist)
    for artifact in (wheel, sdist):
        install_and_smoke_test(artifact.resolve())
    print(f"verified release artifacts: {wheel.name}, {sdist.name}")


if __name__ == "__main__":
    main()
