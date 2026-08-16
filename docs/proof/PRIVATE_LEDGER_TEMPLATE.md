# Private claims ledger template

Keep the completed version outside Git and outside the public evidence bundle.
Use this template for account-specific, employment, customer, financial, or
credential-bearing evidence that cannot be published. The public
`claims_ledger.v1.json` should contain only the redacted claim, its allowed
wording, and the provenance boundary.

For each private claim, record:

```text
claim_id:
private_source_reference:
owner_or_authorship:
observed_date_range:
environment_and_dataset:
metric_definition:
raw_artifact_location:
raw_artifact_digest: sha256:
redacted_public_substitute:
allowed_public_wording:
confidence: high | medium | low
likely_objection:
falsification_test:
retention_or_deletion_date:
review_after:
```

Do not copy raw prompts, provider tokens, authorization headers, account
identifiers, private URLs, order or position exports, customer data, or local
home-directory paths into this file. Store the raw artifact in an
access-controlled system and publish only the sanitized substitute and its
limitations.
