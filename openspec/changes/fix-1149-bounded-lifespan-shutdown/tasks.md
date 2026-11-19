## 1. Bounded shutdown

- [x] 1.1 Add a regression test that swallows the cancel during a refresh and requires shutdown to finish within 5 seconds (fails on the old code).
- [x] 1.2 Stop the refresh loop with a flag set before the cancel, so shutdown is bounded on every Python version.
