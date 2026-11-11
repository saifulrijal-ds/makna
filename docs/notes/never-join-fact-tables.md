A metric built from two tables must aggregate each table separately and then combine; joining the tables row to row gives wrong numbers.

Evidence: `spikes/compiler/output.txt`. Collection rate on the spike data is 0.9411 when payments and installment schedules are aggregated separately, and 1.0006 when they are joined through the contract first, because every payment row is repeated once per installment row.

Rules for the compiler (and for anyone writing reference SQL for the evals):

- One subquery per input metric, from that input's own table, joining only from the "many" side to the "one" side.
- Combine inputs with `UNION ALL` and `GROUP BY` on the shared keys. Do not use `FULL OUTER JOIN`: rows with a NULL dimension value would not match each other.
- A balance (for example outstanding principal) is never summed across snapshot dates. Use the last snapshot date in each period.
- A ratio is computed after combining, never averaged from per-row ratios.
