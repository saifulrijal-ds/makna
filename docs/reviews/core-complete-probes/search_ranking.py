"""Search ranking probe (findings C-3 and open question 8): where the best match of each list
lands after the status and kind weights. Needs the settings of `python -m evals retrieval`.
Run from the repository root:

    uv run python docs/reviews/core-complete-probes/search_ranking.py
"""

from evals.runtime import open_runtime
from makna.search.hybrid import HybridSearch

rt = open_runtime()
search = HybridSearch(rt.engine, rt.provider, rt.search)


def show(query: str, role: str = "viewer", **options: object) -> None:
    result = search.search(query, role=role, limit=10, **options)  # type: ignore[arg-type]
    print(f"== {query!r} role={role} {options} confidence={result.confidence.band}")
    for position, hit in enumerate(result.hits, 1):
        print(
            f"  {position:2d} {hit.object_id:55s} {hit.status:10s} score={hit.score:.4f} "
            f"keyword rank={hit.keyword_rank} vector rank={hit.vector_rank}"
        )


show("region")
show("payment channel")
show("wilayah cabang")
show("customer segment")
show("write-off amount", role="curator")
show("NPL ratio", include_deprecated=True)
show("non-performing loan ratio retired", include_deprecated=True)
show("national id", kinds=["column"])
show("national id", role="curator", kinds=["column"])
