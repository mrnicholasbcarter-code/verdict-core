# Spec: Credit Routes Capacity Classification

## Purpose

Ensure models billed against prepaid credit balances (such as Pollinations / `pol/*`) never count as `FREE` capacity by assumption when pricing is unknown.

## Requirements

1. **Credit Provider Identification**: Routes served by credit providers (Pollinations, `pol`, `g4f-pollinations`) without explicit free evidence must not be assumed free.
2. **Unknown Pricing**: Credit provider routes with unknown pricing must be classified as `CapacityClass.UNKNOWN` (`credit_provider_unknown_pricing`).
3. **Free Community Routes Preserved**: Credit provider routes carrying a `:free` community suffix or free connection signals or all-zero inventory pricing continue to be classified as `CapacityClass.FREE`.
4. **Priced Models Metered**: Credit provider routes with positive inventory pricing are classified as `CapacityClass.METERED`.
