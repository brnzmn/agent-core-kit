---
name: audit-data-sources
description: Audit a dataset, an API provider's coverage, record matches across sources, or source provenance when missing records or unverified origins could invalidate the result.
---

Define the actual input, intended population, source versions, and claim being checked. Preserve raw evidence within allowed storage and use stable record/source identifiers. Do not silently replace unavailable observed data with simulation or generated content.

Choose only the applicable mode:

- For quantities, prices, timestamps, or provider responses, read [numerical contracts](references/numerical-contracts.md).
- For matching records across sources, versioned or quoted source material, or generated text standing in for evidence, read [source reconciliation](references/source-reconciliation.md).

Reconcile expected, discovered, resolved, checked, passed, excluded, and pending records. Explain legitimate exclusions and distinguish missing, invalid, and zero values. Completeness follows the declared applicable population; a successful loop over discovered records does not prove full coverage.

Tie corrections and decisions to inspected evidence. Keep uncertain matches or unsupported claims explicit; do not overwrite conflicting historical decisions without resolving their evidence and identifying the superseding decision.

Use representative boundary cases and existing meaningful checks, respecting the user's restrictions on tests, logs, and data handling. Audit only the requested result and affected contracts; ordinary formatting changes do not need a source audit. Report the verified scope, resulting corrections, and unresolved coverage or provenance limits without promoting partial evidence to a complete pass.
