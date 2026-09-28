# Architecture: the six-stage uplift ladder

The ladder stages, in order, are:

`SEEDED` -> `PROBED` -> `WARRANTED` -> `PACKED` -> `BOUND` -> `SERVED`

`WARRANTED` is the only stage permitted to consult the Core metadata store.
`BOUND` is the only stage that may assert a completed-with identity, and it does so
strictly from the gateway header, never from the echoed body model.
