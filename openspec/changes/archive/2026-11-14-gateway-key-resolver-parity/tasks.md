# Tasks

Owned files: `verdict/orchestration/executors.py` (`DirectGatewayExecutor.__init__`),
`verdict/actions/extra.py` (`_action_credentials_test`), their tests and this change.

- [x] 1.1 Failing test first: with the key only in the owner-only key file, the direct-gateway
  executor sends no bearer credential.
- [x] 1.2 `DirectGatewayExecutor` resolves through `credentials_store.resolve_api_key`, with
  `OPENAI_API_KEY` as the last resort.
- [x] 1.3 Guard tests: an exported key wins over the key file, and `OPENAI_API_KEY` is still
  used when the resolver finds nothing.
- [x] 2.1 Failing test first: with the key only in the owner-only key file, `credentials.test`
  reports `missing` and does not run the live check.
- [x] 2.2 `credentials.test` falls through to `credentials_store.resolve_api_key` for gateway
  key names.
- [x] 2.3 Guard tests: an exported key wins, a non-gateway credential is unchanged, and the
  key value never appears in the action result.
- [x] 3.1 Independent cross-family review of each diff.
