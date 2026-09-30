# Fresh install in clean containers: verdict-core 0.4.1 (2026-09-30)

**What this proves.** Each published one-line install path was run inside a clean container. The container had no `verdict` on `PATH`, no Verdict config and no credentials. Every path installed verdict-core 0.4.1, and the offline `verdict demo` reached `outcome=COMPLETE` with the receipt integrity verified:
- `install.sh`, which downloads the wheel and checks its SHA-256 against the digest pinned in `install.sh` before installing it with pipx;
- `pipx install verdict-core`;
- `uvx --from verdict-core verdict demo`.

**How it was produced.** The GitHub Actions workflow [`.github/workflows/fresh-install.yml`](../../../.github/workflows/fresh-install.yml) produced these files: run [36780227547](https://github.com/mrnicholasbcarter-code/verdict-core/actions/runs/36780227547) on commit `52a3ca2` (the PR merge ref of #770). The workflow uses the `python:3.12-slim` and `python:3.13-slim` images, as root with `HOME=/github/home`. It adds only `curl`, `ca-certificates` and `git`, plus `pipx` or `uv` for the matching path. Anyone can rerun it with **Actions → Fresh install transcript → Run workflow**, and it also runs on every PR that changes `install.sh`. The files below are the uploaded artifacts, with ANSI colour codes removed. Nothing else was edited.

| Transcript | Image | Path | `verdict --version` | Digest check | Exit | Demo outcome |
|---|---|---|---|---|---|---|
| `install.sh-python-3.12-slim.txt` | python:3.12-slim | install.sh | verdict-core verdict 0.4.1 | yes: `[OK] SHA-256 verified for verdict_core-0.4.1-py3-none-any.whl` | `install.sh exit status: 0` | `outcome=COMPLETE` |
| `install.sh-python-3.13-slim.txt` | python:3.13-slim | install.sh | verdict-core verdict 0.4.1 | yes: `[OK] SHA-256 verified for verdict_core-0.4.1-py3-none-any.whl` | `install.sh exit status: 0` | `outcome=COMPLETE` |
| `pipx-python-3.12-slim.txt` | python:3.12-slim | pipx | verdict-core verdict 0.4.1 | n/a (PyPI via pipx/uv) | `verdict demo exit status: 0` | `outcome=COMPLETE` |
| `pipx-python-3.13-slim.txt` | python:3.13-slim | pipx | verdict-core verdict 0.4.1 | n/a (PyPI via pipx/uv) | `verdict demo exit status: 0` | `outcome=COMPLETE` |
| `uvx-python-3.12-slim.txt` | python:3.12-slim | uvx | verdict-core verdict 0.4.1 | n/a (PyPI via pipx/uv) | `uvx exit status: 0` | `outcome=COMPLETE` |
| `uvx-python-3.13-slim.txt` | python:3.13-slim | uvx | verdict-core verdict 0.4.1 | n/a (PyPI via pipx/uv) | `uvx exit status: 0` | `outcome=COMPLETE` |

**What this does NOT prove.**
- macOS or Windows installs. Only Linux (Debian-based python images) was tested.
- A non-root user. The containers run as root, so the `pip install --user` fallback in `install.sh` was not exercised.
- Any live provider path. `verdict demo` is the offline scenario: scripted workers, an injected rate limit, a scripted reviewer, and no model calls.
- A published Verdict container image or Codespaces (BOD-250's other two items).
