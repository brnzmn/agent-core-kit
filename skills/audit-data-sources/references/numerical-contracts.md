# Numerical and provider contracts

Establish units, precision, currency, timezone, timestamp meaning, aggregation interval, ordering, and duplicate policy before calculating. Check quantities and totals at each transformation boundary; define the conservation identities that apply, such as opening balance plus inflows minus outflows equals closing balance, with fees and rounding explicit.

Inspect requested versus returned coverage, pagination, provider caps, source timestamps, and freshness. A request for 100 samples that returns four has four observed samples until more are retrieved; interpolation must remain labeled. Preserve missing and invalid values rather than converting NaN or absent values to zero.

Make timestamp alignment explicit: event time versus observation time, nearest/previous match, maximum allowed distance, and treatment of unavailable values. For historical comparisons, exclude future information and keep recorded events separate from simulated ones, modelled choices, and assumptions.

Exercise representative source responses: empty, sparse, duplicated, out of order, malformed, and unit changes as relevant to the provider contract. Compare computed totals with raw representative records. Report coverage denominators and any unresolved observations alongside the result.
