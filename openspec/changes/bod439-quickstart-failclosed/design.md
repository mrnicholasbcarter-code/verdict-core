# Design

## Context

The route step already runs a child `TestClient` POST and prints the status code, then the JSON body. The parent used to continue whenever that child exited 0. See proposal.md for the observed false `RESULT: PASS`.

## Goals / Non-Goals

**Goals:** Report the route probe as success only when the child exits 0, the first stdout line is `200`, and the JSON `decision` is a non-empty string other than `denied`.

**Non-Goals:** Change `/v1/route` auth, the probe payload, or TestClient lifespan. Do not waive the PyPI claim or G7.1. Do not close the whole MVP.

## Decisions

Keep the child command and the API contract. Add a small parent check, `_route_probe_succeeded`, over the existing child stdout:

- return code is 0
- first line is exactly `200`
- the rest parses as one JSON object
- `decision` is a non-empty string and is not `denied`

Anything else calls the existing `sys.exit(1)` path. `if __name__ == "__main__"` already prints `RESULT: FAIL` for a non-zero `SystemExit`.

`denied` is the failure decision already used by route responses. Success is any other non-empty decision string, not one invented model name.

Drive `main` with a fake `run` and a no-op temp directory. No network and no install. Negative cases cover 503, 422, malformed JSON, a non-object body, a missing or empty decision, `denied`, and a non-zero child. One positive case is HTTP 200 with `decision: selected`.

## Risks / Trade-offs

A non-denied decision string could be mistaken for a finished product proof → keep G7.1, the PyPI claim, and whole-MVP closeout out of this change. A stricter parent check could reject a real 200 that the old code accepted → accept only the stated shape and leave auth, payload, and lifespan unchanged.

## Security / trust implications

A failed, denied, or malformed route probe must not be reported as a successful quickstart. This check does not add an anonymous auth bypass, change API auth, or treat a local pass as installed-main trust.

## Routing / context / memory implications

The probe still uses the existing route request. No routing policy, context pipeline, or memory store changes. A passing local probe is not retained operational proof.

## Migration / rollback

No data migration. Revert the route-step success check to restore the previous continue-on-child-exit-0 behavior. Installed-main setup, doctor, and BOD-439 operational acceptance remain OPEN.

## ADR impact

ADR-007 remains governing: a route observation is not by itself readiness or product completion. ADR-030 and ADR-031 remain governing for how this change is specified and accepted; this artifact correction does not amend them. No ADR file changes. G7.1 and the PyPI claim stay outside this slice.
