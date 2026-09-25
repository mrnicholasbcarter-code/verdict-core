# Tasks

## 1. OpenSpec Foundation

- [ ] 1.1 Verify OpenSpec 1.13.2 is latest version with `npm view @fission-ai/openspec version`
- [ ] 1.2 Initialize OpenSpec with `npx -y @fission-ai/openspec@1.13.2 init --tools none` and verify openspec/config.yaml created
- [ ] 1.3 Fork spec-driven schema to verdict-change-v1 and verify openspec/schemas/verdict-change-v1/ created

## 2. Schema Customization

- [ ] 2.1 Update openspec/config.yaml schema field to verdict-change-v1 and add Verdict project context
- [ ] 2.2 Review forked templates (proposal/spec/design/tasks.md) match BOD-167 field requirements
- [ ] 2.3 Verify schema with `npx -y @fission-ai/openspec@1.13.2 schema validate verdict-change-v1`

## 3. Deterministic Validator

- [ ] 3.1 Create scripts/validate_openspec_change.py using stdlib only (no external imports)
- [ ] 3.2 Implement artifact presence checks (proposal.md, specs/, design.md, tasks.md)
- [ ] 3.3 Implement required heading checks for each artifact
- [ ] 3.4 Implement evidence label check (KNOWN/INFERRED/NOT AVAILABLE in proposal.md)
- [ ] 3.5 Implement skip_specs support (.openspec.yaml with skip_specs: true)
- [ ] 3.6 Verify validator exits 0 on valid input, 1 on invalid with precise reason

## 4. Sample and Fixtures

- [ ] 4.1 Create valid sample change at openspec/changes/bod-204-openspec-foundation/ (this change)
- [ ] 4.2 Create malformed fixture at tests/fixtures/openspec/malformed-change/ (missing required field)
- [ ] 4.3 Run validator on sample and verify exit 0
- [ ] 4.4 Run validator on malformed fixture and verify exit 1 with expected error

## 5. Test Suite

- [ ] 5.1 Create tests/test_openspec_change.py using unittest.TestCase
- [ ] 5.2 Add test_valid_change_passes_validation
- [ ] 5.3 Add test_malformed_change_fails_validation
- [ ] 5.4 Add test_missing_artifact_fails with subTests for each artifact
- [ ] 5.5 Add test_missing_heading_fails with subTests for required headings
- [ ] 5.6 Add test_missing_evidence_labels_fails
- [ ] 5.7 Run `python3 -m unittest discover -s tests -v` and verify all pass

## 6. Documentation

- [ ] 6.1 Create docs/OPENSPEC.md with authority boundaries section and verify all sections present
- [ ] 6.2 Add significant-change triggers and exemptions to OPENSPEC.md
- [ ] 6.3 Add BOD-167 to verdict-change-v1 field mapping table
- [ ] 6.4 Add exact commands section (init, fork, validate, change creation)
- [ ] 6.5 Add verified agent integrations note (none for --tools none)
- [ ] 6.6 Add "no mass migration" statement clearly
- [ ] 6.7 Link OPENSPEC.md from README.md (one line in documentation section)
- [ ] 6.8 Add superseded note to docs/SPEC_KIT_LITE.md pointing to OPENSPEC.md

## 7. CI and Validation

- [ ] 7.1 Run python3 scripts/check_local_links.py and verify pass
- [ ] 7.2 Run python3 -m unittest discover -s tests -v and verify pass  
- [ ] 7.3 Run python3 scripts/check_adr_lifecycle.py and verify pass
- [ ] 7.4 Run python3 scripts/verify_adr_sources.py and verify pass
- [ ] 7.5 Run python3 scripts/check_ownership_phrases.py and verify pass
- [ ] 7.6 Run python3 scripts/secret_scan.py . and verify pass
- [ ] 7.7 Run python3 scripts/inventory_check.py and verify pass
- [ ] 7.8 Run git diff --check and verify pass

## 8. Fresh Clone Proof

- [ ] 8.1 Push branch to origin
- [ ] 8.2 Clone branch into /tmp directory
- [ ] 8.3 Run validator on sample change and verify exit code 0 with captured output
- [ ] 8.4 Run validator on malformed fixture and capture output
- [ ] 8.5 Record exact commands, outputs, and head SHA for PR body

## 9. PR Creation

- [ ] 9.1 Commit all changes with multi-line message via `git commit -F <file>`
- [ ] 9.2 Push branch with `git push -u origin feat/bod-204-openspec-foundation`
- [ ] 9.3 Create PR with `gh pr create --title "feat(BOD-204): adopt OpenSpec with verdict-change-v1 schema + deterministic validator"`
- [ ] 9.4 Include fresh-clone proof outputs in PR body
- [ ] 9.5 Reply to parent with PR number, head SHA, OpenSpec version, schema mechanism, diff stat, check results
