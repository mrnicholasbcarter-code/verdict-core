# Setup preview contract

`verdict setup --dry-run --json` produces a mutation-free `setup_plan`.
Every proposed action includes its reason, security impact, postcondition, and
exact undo description. The preview does not read configuration contents,
probe optional tools, contact the network, or access credentials.

An existing configuration is preserved as a read-only action. A missing
configuration is presented as a consent-required creation action. Applying
either action remains a separate transactional boundary; the preview itself
does not authorize or perform mutations.

Host prerequisites (the pinned `ocr`, the metadata, acceptance and identify
timers, the `verdict serve` unit and a missing Prime registry) are not part of
this preview, because finding them means looking at the host. `verdict setup
--recommended` previews them without changes: it reads PATH, the `ocr` file
digest (running `ocr --version` only when the digest differs), systemd user
unit state and files, and whether `serve.env` holds the expected keys (never
their values). APPLY then needs `--yes`, or `--allow
<id>` per action under `--non-interactive`. The ids and what each one installs
are listed under `verdict setup` in [CLI_REFERENCE.md](../CLI_REFERENCE.md).
