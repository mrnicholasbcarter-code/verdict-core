# Synthetic Calibration Records

This directory contains synthetic but realistic OpenJev calibration records for BOD-203.

## Important

**These are NOT real measurements.** They are generated for testing and calibration
purposes only. Do not use these for production analysis or assume they reflect
actual OpenJev performance on real tasks.

## Record schema

Each line is a JSON object with the fields defined in `verdict.decision_signals.calibration.CalibrationRecord`:

- `task_id`: Unique identifier
- `task_class`: One of `verdict.decision_signals.calibration.TASK_CLASSES`
- `frontier_worthy`: OpenJev probability (0-1) or `null` if no usable signal
- `confidence`: Signal confidence (0-1) or `null`
- `security_sensitive`: Security sensitivity score (0-1) or `null`
- `security_relevant`: Ground truth - whether security context matters
- `planner_was_frontier`: What Verdict's planner actually used
- `verified`: Whether the task completed successfully
- `first_pass`: Whether first attempt succeeded
- `retries`, `escalations`, `total_cost_usd`, `time_to_green_s`, `signal_latency_ms`: Optional metrics
- `outcome`, `excluded_reason`, `capacity_class`, `capability_tier`, `attempts`, `lifts`: labels from
  verified outcomes (OJ1, #1220). **These records predate them**, so all are unset here. They also
  still carry a `frontier_needed` key from the removed topology label; the loader ignores it.

## Synthetic design

- 60+ records spanning all TASK_CLASSES
- One security false negative
- Mix of low-confidence signals to test min_confidence filtering
