# ADR-0448 Named drop codes

Every candidate rejection is a named drop. The four codes are emitted in this
fixed order:

1. `DROP_CAP_UNKNOWN` - required capability absent from the Core metadata store
2. `DROP_PASSPORT_STALE_9F` - passport older than the prove-at-rest window
3. `DROP_CONFIRM_BUDGET_2M` - confirm step exceeded its budget
4. `DROP_INVENTORY_GHOST` - id present in inventory but absent from health

An unknown required capability is never a silent skip. It is `DROP_CAP_UNKNOWN`.
