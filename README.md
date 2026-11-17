# Makna

An open semantic layer platform: one governed place where people and AI agents find and use the meaning of data. Makna is Indonesian for "meaning".

Status: design complete, build not started. Start with `docs/PROJECT_BRIEF.md`, then read `docs/ARCHITECTURE.md` (the design), `docs/DECISIONS.md` (why), and `docs/PLAN.md` (the work packages).

## Limits of the personal-data protection

Personal data is protected by Makna's rules for the SQL it compiles: a personal dimension is returned only masked, cannot be filtered on, and only counts can be taken over it (`docs/ARCHITECTURE.md` section 7.6). When a request selects a personal dimension, groups built from fewer than the configured minimum number of records are left out of that result. That minimum prevents small cells within one result. It is not a privacy guarantee, and it does not protect a person from an agent that tries: two requests that differ by one person can be compared, and requests grouped by non-personal dimensions alone are not covered by the rule. Access control in the query engine is the protection for personal data; the people and agents that can reach the engine decide what they may read.
