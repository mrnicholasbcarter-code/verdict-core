# Plan

1. Reuse the five existing `.prime/agent/skills` as the lifecycle boundary.
2. Keep deterministic state/lease/proof contracts in standard-library helpers under `scripts/`.
3. Keep Prime process supervision external to model turns and use the existing Prime/OmniRoute/Linear/GitHub authorities.
4. Validate with focused unit tests, subprocess failure injection, Prime discovery, strict mypy/Ruff, and the repository CI commands.
5. Record evidence outside tracked source under the git common `verdict-prime` state directory.
