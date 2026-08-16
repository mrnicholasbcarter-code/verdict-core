# Setup preview contract

`verdict setup --dry-run --json` produces a mutation-free `setup_plan`.
Every proposed action includes its reason, security impact, postcondition, and
exact undo description. The preview does not read configuration contents,
probe optional tools, contact the network, or access credentials.

An existing configuration is preserved as a read-only action. A missing
configuration is presented as a consent-required creation action. Applying
either action remains a separate transactional boundary; the preview itself
does not authorize or perform mutations.
