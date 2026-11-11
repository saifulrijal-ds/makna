Hitting the section 2 row targets for payments needs origination volumes that fall over time, not a flat rate.

With uniform application dates the generator gave about 51,000 payments against a target of 90,000: a contract disbursed uniformly over 30 months has on average 15 months of installments due, and tenors and closures cut that further. A power of 2 on the registration and repeat-application dates (`ORIGINATION_SKEW` in `data/generator/entities.py`) brings payments to about 76,000 and snapshots to about 81,000 (both within 25%).

- Child table sizes (schedules, payments, snapshots, collection actions) are not planned: they follow from the lifecycle, so only a full generation gives them. The test for them uses the full-scale dataset.
- Quirk shares (cancelled, transferred, restructured, behaviour classes) use exact allocation (`exact_allocation`), so the rates in the Q checks hold at every scale and do not depend on sampling noise.
- Collection action volume is tuned by `ACTION_PROBABILITY` in `data/generator/lifecycle.py`; re-check the count at scale 1.0 after changing anything that moves late installments.
