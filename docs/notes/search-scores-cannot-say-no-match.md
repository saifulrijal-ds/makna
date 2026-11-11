Search rank scores cannot tell "nothing matches" from a weak valid match; return evidence and a confidence band instead of a yes or no.

Evidence: `spikes/search/output.txt`. The query "employee attrition rate" (no such metric) got a higher keyword score than several valid queries, because generic words such as "rate" and "ratio" occur in many metric names.

- Do not gate "no match" on a full-text score or on a fused rank score.
- Coverage of the query's informative words by the names and synonyms of the top hits, weighted by inverse document frequency, separates better but still overlaps. Use it for the confidence band (`docs/ARCHITECTURE.md` section 8.3) and calibrate thresholds on the golden set.
- Fusing a weak ranked list with a strong one can lower first-result accuracy (0.88 to 0.75 in the spike). Keep the fusion weights in configuration and always report each leg alone next to the fused score.
- The final decision to abstain belongs to the caller, who can read the candidate descriptions. Tool descriptions and response guidance must tell the agent to say "no approved definition exists" and not to substitute a similar metric.
