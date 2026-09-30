# Fresh install in clean containers: verdict-core 0.4.1 (2026-09-30)

**What this shows.** Each published install path ran inside a clean container, installed verdict-core 0.4.1, and ran the offline `verdict demo`. The demo reached a `run_finished … outcome=COMPLETE` event, and the receipt integrity check passed. The paths:
- `install.sh`: it downloads the wheel and checks its SHA-256 against the digest pinned in `install.sh` before installing it with pipx;
- `pipx install verdict-core`;
- `uvx --from verdict-core verdict demo`.

Each transcript records these facts before the install:
- no `verdict` on `PATH`;
- no `~/.config/verdict` or `~/.verdict` directory;
- no environment variable whose name starts with `OMNIROUTE_`, `LLMGATE_`, `VERDICT_`, `OPENAI`, `ANTHROPIC`, `GEMINI`, `GOOGLE_API` or `OPENROUTER`. The header prints only names, and the list before the `none-listed-above` marker is empty.

**How it was produced.** The workflow [`.github/workflows/fresh-install.yml`](../../../.github/workflows/fresh-install.yml) produced these files in run [36780811595](https://github.com/mrnicholasbcarter-code/verdict-core/actions/runs/36780811595), a `pull_request` run of #770 on merge ref `2ad0894`, with the images `python:3.12-slim` and `python:3.13-slim` (Debian 13, Python 3.12.14 / 3.13.15). The containers run as root with `HOME=/github/home`. The job adds only `curl`, `ca-certificates` and `git`, plus `pipx` or `uv` for the matching path. It strips ANSI colour codes itself and logs each transcript's `sha256sum`. A job passes only if its transcript has an exact `run_finished … outcome=COMPLETE` line and the exact receipt-verification line. The files here are the downloaded artifacts, committed unchanged. Their SHA-256 digests are below; they match the digests the job logged.

| Transcript | Image | Path | `verdict --version` | Digest check | Exit | Demo outcome |
|---|---|---|---|---|---|---|
| `install.sh-python-3.12-slim.txt` | python:3.12-slim | install.sh | verdict-core verdict 0.4.1 | `[OK] SHA-256 verified for verdict_core-0.4.1-py3-none-any.whl` | `install.sh exit status: 0` | `outcome=COMPLETE` |
| `install.sh-python-3.13-slim.txt` | python:3.13-slim | install.sh | verdict-core verdict 0.4.1 | `[OK] SHA-256 verified for verdict_core-0.4.1-py3-none-any.whl` | `install.sh exit status: 0` | `outcome=COMPLETE` |
| `pipx-python-3.12-slim.txt` | python:3.12-slim | pipx | verdict-core verdict 0.4.1 | not applicable (installed from PyPI by pipx/uv) | `verdict demo exit status: 0` | `outcome=COMPLETE` |
| `pipx-python-3.13-slim.txt` | python:3.13-slim | pipx | verdict-core verdict 0.4.1 | not applicable (installed from PyPI by pipx/uv) | `verdict demo exit status: 0` | `outcome=COMPLETE` |
| `uvx-python-3.12-slim.txt` | python:3.12-slim | uvx | verdict-core verdict 0.4.1 | not applicable (installed from PyPI by pipx/uv) | `uvx exit status: 0` | `outcome=COMPLETE` |
| `uvx-python-3.13-slim.txt` | python:3.13-slim | uvx | verdict-core verdict 0.4.1 | not applicable (installed from PyPI by pipx/uv) | `uvx exit status: 0` | `outcome=COMPLETE` |

```
ac4c2e718ddebebbfe061e43b580209cb1ab498f2e16cd2cc2e7e8a4b01d67d6  install.sh-python-3.12-slim.txt
3ebd2bdb928822f42418cf3aae19cd54e6ee85d24a385669f49e9a4c576da775  install.sh-python-3.13-slim.txt
a136b058a3e2bb0073e35ee7286285cbac7df0398da4263558e3d7cb3281251c  pipx-python-3.12-slim.txt
0cc6354a2b93519c09906044ebc5cc452537b7d523f9b36311a50113600bb13f  pipx-python-3.13-slim.txt
ca03cb1ec62fbf1c2df75778bf5817cc7978d1adb0c5801131a092b14b725278  uvx-python-3.12-slim.txt
4553b0386e76cb2034e65b9fcc1b14200c5207b539fb8d9b6039840b5374531a  uvx-python-3.13-slim.txt
```

To rerun: **Actions → Fresh install transcript → Run workflow**, optionally with a `version`. The job also runs on every PR that changes `install.sh` or the workflow.

**What this does NOT prove.**
- The one-line `curl … | bash` form. `install.sh` ran from the PR checkout, not fetched from a URL.
- Anything beyond merge ref `2ad0894` and the image digests pulled on 2026-09-30. A later commit, image or PyPI state needs a new run.
- macOS or Windows. Only Linux (Debian 13 python images) was tested.
- A non-root user. The containers run as root, so the `pip install --user` fallback in `install.sh` did not run.
- Any live provider path. `verdict demo` is the offline scenario: scripted workers, an injected rate limit and a scripted reviewer, with no model calls. The pre-install checks cover only the variable names listed above. The runner's own `GITHUB_TOKEN` is used by checkout and is not in that list.
- A published Verdict container image or Codespaces (BOD-250's other two items).
