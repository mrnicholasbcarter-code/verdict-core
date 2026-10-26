# Design

## Context
The route step already runs a child `TestClient` POST and prints the status code, then the JSON body. The parent used to continue whenever that child exited 0.

## Decision
Keep the child command and the API contract. Add a small parent check, `_route_probe_succeeded`, over the existing child stdout:

- return code is 0
- first line is exactly `200`
- the rest parses as one JSON object
- `decision` is a non-empty string and is not `denied`

Anything else calls the existing `sys.exit(1)` path. `if __name__ == "__main__"` already prints `RESULT: FAIL` for a non-zero `SystemExit`.

`denied` is the failure decision already used by route responses. Success is any other non-empty decision string, not one invented model name.

## Test strategy
Drive `main` with a fake `run` and a no-op temp directory. No network and no install. Negative cases cover 503, 422, malformed JSON, a non-object body, a missing or empty decision, `denied`, and a non-zero child. One positive case is HTTP 200 with `decision: selected`.

## Non-goals
Do not change `/v1/route` auth, the probe payload, or TestClient lifespan. Do not waive the PyPI claim or G7.1.
