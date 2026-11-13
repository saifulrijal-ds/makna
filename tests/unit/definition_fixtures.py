"""A small valid definitions repository, and one minimal mutation per validation rule.

`base()` returns the files of a tiny `shop` domain as {relative path: decoded YAML}. Each
fixture below is a function that edits that dict, so the difference between the valid set and a
broken one is the few lines in the function. `write()` turns a file dict into a directory.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

Files = dict[str, dict[str, Any]]
Mutation = Callable[[Files], None]

OWNER = {"team": "sales"}
SYN = {"en": ["thing"], "id": ["benda"]}


def _object(kind: str, name: str, **fields: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "apiVersion": "makna/v1",
        "kind": kind,
        "name": name,
        "label": name.replace("_", " ").title(),
        "description": f"The {name.replace('_', ' ')} of the shop, written for a business reader.",
        "owner": OWNER,
        "status": "approved",
        "sensitivity": "internal",
    }
    if kind != "domain":
        doc["domain"] = "shop"
        doc["synonyms"] = SYN
    doc.update(fields)
    return doc


def _dim(name: str, type_: str, **fields: Any) -> dict[str, Any]:
    return {"name": name, "type": type_, "description": f"The {name} of the row.", **fields}


def _measure(name: str, agg: str, time_dimension: str, **fields: Any) -> dict[str, Any]:
    return {
        "name": name,
        "agg": agg,
        "time_dimension": time_dimension,
        "description": f"The {name} of the entity.",
        **fields,
    }


def _rel(name: str, to: str, key: str) -> dict[str, Any]:
    return {
        "name": name,
        "to": to,
        "keys": [{"from": key, "to": key}],
        "description": f"The {to} of the row.",
    }


def _metric(name: str, **fields: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "type": "simple",
        "format": {"kind": "number"},
        "example_questions": [f"What is {name}?"],
        "example_requests": [{"question": f"{name} by month", "request": {"grain": "month"}}],
    }
    return _object("metric", name, **{**defaults, **fields})


def base() -> Files:
    """A valid repository: 1 domain, 3 entities, 6 metrics, 1 glossary term."""
    return {
        "shop/domain.yaml": _object("domain", "shop"),
        "shop/entities/region.yaml": _object(
            "entity",
            "region",
            source={"table": "shop.core.regions"},
            primary_key=["region_id"],
            dimensions=[
                _dim("region_name", "categorical", values=[{"value": "north"}, {"value": "south"}]),
            ],
        ),
        "shop/entities/customer.yaml": _object(
            "entity",
            "customer",
            sensitivity="confidential",
            source={"table": "shop.core.customers"},
            primary_key=["customer_id"],
            dimensions=[
                _dim(
                    "segment", "categorical", sensitivity="internal", values=[{"value": "retail"}]
                ),
                _dim("registered_date", "time", sensitivity="internal"),
                _dim("email", "categorical", sensitivity="personal", masking={"method": "redact"}),
                _dim(
                    "birth_year",
                    "time",
                    expr="birth_date",
                    sensitivity="personal",
                    masking={"method": "truncate_date", "grain": "year"},
                ),
            ],
            measures=[
                _measure("signups", "count", "registered_date"),
                _measure("distinct_emails", "count_distinct", "registered_date", expr="email"),
            ],
            relationships=[_rel("home_region", "region", "region_id")],
        ),
        "shop/entities/order.yaml": _object(
            "entity",
            "order",
            source={"table": "shop.core.orders"},
            primary_key=["order_id"],
            dimensions=[
                _dim("order_date", "time"),
                _dim(
                    "status",
                    "categorical",
                    values=[{"value": "open"}, {"value": "shipped"}, {"value": "cancelled"}],
                ),
                _dim("is_gift", "boolean"),
                _dim("item_count", "numeric", masking={"method": "bucket", "width": 5}),
            ],
            rules=[
                {
                    "name": "big_order",
                    "description": "An order worth more than one thousand.",
                    "expr": "total_amount > 1000",
                }
            ],
            measures=[
                _measure("revenue", "sum", "order_date", expr="total_amount"),
                _measure("orders", "count", "order_date"),
                _measure("open_balance", "sum", "order_date", expr="balance", semi_additive="last"),
            ],
            relationships=[_rel("customer", "customer", "customer_id")],
        ),
        "shop/metrics/revenue.yaml": _metric(
            "revenue",
            measure="order.revenue",
            filters=[{"dimension": "order.status", "op": "!=", "value": "cancelled"}],
            join_paths={"region": ["order.customer", "customer.home_region"]},
        ),
        "shop/metrics/order_count.yaml": _metric(
            "order_count",
            measure="order.orders",
            filters=[{"rule": "order.big_order"}],
        ),
        "shop/metrics/inventory_balance.yaml": _metric(
            "inventory_balance",
            measure="order.open_balance",
            time_grains=["month", "quarter", "year"],
            default_grain="month",
        ),
        "shop/metrics/revenue_per_order.yaml": _metric(
            "revenue_per_order", type="ratio", numerator="revenue", denominator="order_count"
        ),
        "shop/metrics/average_order_value.yaml": _metric(
            "average_order_value",
            type="derived",
            expr="revenue / NULLIF(order_count, 0)",
            inputs=["revenue", "order_count"],
        ),
        "shop/glossary/aov.yaml": _object(
            "glossary_term", "aov", related=["metric:average_order_value"]
        ),
    }


def write(files: Files, root: Path) -> Path:
    for relative, doc in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return root


def fixture_dir(mutation: Mutation, root: Path) -> Path:
    files = copy.deepcopy(base())
    mutation(files)
    return write(files, root)


# --- Mutation helpers ---


def _move(files: Files, old: str, new: str) -> dict[str, Any]:
    doc = files.pop(old)
    files[new] = doc
    return doc


def _entity(files: Files, name: str) -> dict[str, Any]:
    return files[f"shop/entities/{name}.yaml"]


def _metric_doc(files: Files, name: str) -> dict[str, Any]:
    return files[f"shop/metrics/{name}.yaml"]


def _named(items: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(i for i in items if i["name"] == name)


# --- V01 ---


def v01_metric_without_id_synonym(files: Files) -> None:
    del _metric_doc(files, "order_count")["synonyms"]["id"]


def v01_unknown_key(files: Files) -> None:
    files["shop/glossary/aov.yaml"]["colour"] = "red"


# --- V02 ---


def v02_file_name_differs_from_name(files: Files) -> None:
    _move(files, "shop/metrics/order_count.yaml", "shop/metrics/orders_total.yaml")


def v02_wrong_domain_folder(files: Files) -> None:
    files["other/domain.yaml"] = _object("domain", "other")
    _move(files, "shop/glossary/aov.yaml", "other/glossary/aov.yaml")


def v02_official_file_with_draft_status(files: Files) -> None:
    files["shop/glossary/aov.yaml"]["status"] = "draft"


def v02_proposal_file_with_approved_status(files: Files) -> None:
    files["shop/glossary/aov.draft.yaml"] = copy.deepcopy(files["shop/glossary/aov.yaml"])


# --- V03 ---


def v03_same_metric_name_in_two_files(files: Files) -> None:
    files["other/domain.yaml"] = _object("domain", "other")
    twin = copy.deepcopy(_metric_doc(files, "order_count"))
    twin["domain"] = "other"
    files["other/metrics/order_count.yaml"] = twin


def v03_duplicate_dimension_name(files: Files) -> None:
    dims = _entity(files, "region")["dimensions"]
    dims.append(copy.deepcopy(dims[0]))


def v03_duplicate_measure_name(files: Files) -> None:
    measures = _entity(files, "order")["measures"]
    measures.append(copy.deepcopy(measures[0]))


def v03_duplicate_relationship_name(files: Files) -> None:
    rels = _entity(files, "customer")["relationships"]
    rels.append(copy.deepcopy(rels[0]))


def v03_dimension_name_with_a_double_underscore(files: Files) -> None:
    dims = _entity(files, "region")["dimensions"]
    dims.append({**copy.deepcopy(dims[0]), "name": "region__code", "values": None})
    dims[-1].pop("values")


def v03_entity_name_with_a_double_underscore(files: Files) -> None:
    twin = copy.deepcopy(_entity(files, "region"))
    twin.update(name="ware__house", source={"table": "shop.core.warehouses"})
    files["shop/entities/ware__house.yaml"] = twin


# --- V04 ---


def v04_domain_does_not_exist(files: Files) -> None:
    doc = _move(files, "shop/glossary/aov.yaml", "nowhere/glossary/aov.yaml")
    doc["domain"] = "nowhere"


# --- V05 ---


def v05_dimension_with_aggregate(files: Files) -> None:
    _entity(files, "region")["dimensions"][0]["expr"] = "SUM(region_name)"


def v05_measure_with_subquery(files: Files) -> None:
    measure = _named(_entity(files, "order")["measures"], "revenue")
    measure["expr"] = "(SELECT MAX(x) FROM other)"


def v05_rule_with_window_function(files: Files) -> None:
    _entity(files, "order")["rules"][0]["expr"] = "ROW_NUMBER() OVER (ORDER BY total_amount) = 1"


def v05_expression_does_not_parse(files: Files) -> None:
    measure = _named(_entity(files, "order")["measures"], "orders")
    measure["filter"] = "total_amount >"


def v05_two_expressions_in_one(files: Files) -> None:
    _entity(files, "region")["dimensions"][0]["expr"] = "region_name; region_id"


# --- V06 ---


def v06_time_dimension_is_not_a_time_dimension(files: Files) -> None:
    _named(_entity(files, "order")["measures"], "orders")["time_dimension"] = "status"


def v06_time_dimension_does_not_exist(files: Files) -> None:
    _named(_entity(files, "order")["measures"], "orders")["time_dimension"] = "shipped_date"


# --- V07 ---


def v07_relationship_to_missing_entity(files: Files) -> None:
    _entity(files, "customer")["relationships"][0]["to"] = "warehouse"
    # keep the V09 join path of `revenue` out of the picture: it no longer ends at region
    del _metric_doc(files, "revenue")["join_paths"]


# --- V08 ---


def v08_simple_measure_does_not_resolve(files: Files) -> None:
    _metric_doc(files, "order_count")["measure"] = "order.parcels"


def v08_simple_measure_entity_does_not_exist(files: Files) -> None:
    _metric_doc(files, "order_count")["measure"] = "parcel.orders"


def v08_ratio_input_is_not_a_metric(files: Files) -> None:
    _metric_doc(files, "revenue_per_order")["denominator"] = "no_such_metric"


def v08_ratio_cycle(files: Files) -> None:
    _metric_doc(files, "revenue_per_order")["numerator"] = "average_order_value"
    _metric_doc(files, "average_order_value")["inputs"] = ["revenue_per_order", "order_count"]
    _metric_doc(files, "average_order_value")["expr"] = "revenue_per_order / NULLIF(order_count, 0)"


def v08_derived_expression_uses_name_outside_inputs(files: Files) -> None:
    _metric_doc(files, "average_order_value")["expr"] = "revenue / NULLIF(inventory_balance, 0)"


def v08_derived_expression_uses_a_function(files: Files) -> None:
    _metric_doc(files, "average_order_value")["expr"] = "ROUND(revenue / order_count)"


# --- V09 ---


def v09_chain_does_not_start_at_the_measure_entity(files: Files) -> None:
    _metric_doc(files, "revenue")["join_paths"]["region"] = ["customer.home_region"]


def v09_chain_is_not_connected(files: Files) -> None:
    _metric_doc(files, "revenue")["join_paths"]["region"] = ["order.customer", "order.customer"]


def v09_chain_ends_at_another_entity(files: Files) -> None:
    _metric_doc(files, "revenue")["join_paths"] = {"region": ["order.customer"]}


def v09_relationship_does_not_exist(files: Files) -> None:
    _metric_doc(files, "revenue")["join_paths"]["region"] = ["order.buyer", "customer.home_region"]


# --- V10 ---


def v10_filter_on_personal_dimension(files: Files) -> None:
    _metric_doc(files, "revenue")["filters"] = [
        {"dimension": "customer.email", "op": "=", "value": "a@example.com"}
    ]


def v10_filter_value_of_wrong_type(files: Files) -> None:
    _metric_doc(files, "revenue")["filters"] = [
        {"dimension": "order.is_gift", "op": "=", "value": 1}
    ]


def v10_filter_value_not_in_values(files: Files) -> None:
    _metric_doc(files, "revenue")["filters"][0]["value"] = "returned"


def v10_filter_dimension_not_reachable(files: Files) -> None:
    # region is reachable from order; order is not reachable from region.
    _metric_doc(files, "revenue")["measure"] = "customer.signups"
    _metric_doc(files, "revenue")["join_paths"] = {"region": ["customer.home_region"]}


def v10_filter_on_unknown_dimension(files: Files) -> None:
    _metric_doc(files, "revenue")["filters"][0]["dimension"] = "order.state"


def v10_date_filter_with_text(files: Files) -> None:
    _metric_doc(files, "revenue")["filters"] = [
        {"dimension": "customer.registered_date", "op": ">=", "value": "last spring"}
    ]


# --- V11 ---


def v11_truncate_date_on_categorical(files: Files) -> None:
    dim = _named(_entity(files, "customer")["dimensions"], "email")
    dim["masking"] = {"method": "truncate_date", "grain": "year"}


def v11_bucket_on_time(files: Files) -> None:
    dim = _named(_entity(files, "customer")["dimensions"], "birth_year")
    dim["masking"] = {"method": "bucket", "width": 5}


def v11_redact_on_numeric(files: Files) -> None:
    _named(_entity(files, "order")["dimensions"], "item_count")["masking"] = {"method": "redact"}


# --- V12 ---


def v12_sum_over_personal_column(files: Files) -> None:
    _named(_entity(files, "customer")["measures"], "distinct_emails")["agg"] = "max"


def v12_filter_on_personal_column_of_a_sum(files: Files) -> None:
    measures = _entity(files, "customer")["measures"]
    measures.append(
        _measure("big_domains", "sum", "registered_date", expr="1", filter="email <> ''")
    )


def v12_count_over_an_expression_of_a_personal_column(files: Files) -> None:
    measures = _entity(files, "customer")["measures"]
    measures.append(
        _measure(
            "early_signups",
            "count",
            "registered_date",
            expr="CASE WHEN email < 'm' THEN 1 END",
        )
    )


def v12_count_with_a_filter_on_a_personal_column(files: Files) -> None:
    measures = _entity(files, "customer")["measures"]
    measures.append(_measure("mailed", "count", "registered_date", filter="email <> ''"))


# --- V13 ---


def v13_approved_metric_depends_on_draft_metric(files: Files) -> None:
    doc = _move(files, "shop/metrics/order_count.yaml", "shop/metrics/order_count.draft.yaml")
    doc["status"] = "draft"


def v13_in_review_metric_depends_on_draft_entity(files: Files) -> None:
    region = _move(files, "shop/entities/region.yaml", "shop/entities/region.draft.yaml")
    region["status"] = "draft"


def v13_approved_metric_depends_on_deprecated_metric(files: Files) -> None:
    _metric_doc(files, "order_count").update(
        status="deprecated", deprecation={"reason": "Replaced by a better count."}
    )


# --- V14 ---


def v14_deprecated_without_reason(files: Files) -> None:
    files["shop/glossary/aov.yaml"]["status"] = "deprecated"


def v14_replaced_by_does_not_resolve(files: Files) -> None:
    doc = files["shop/glossary/aov.yaml"]
    doc["status"] = "deprecated"
    doc["deprecation"] = {"reason": "Merged.", "replaced_by": "glossary_term:nothing"}


def v14_replaced_by_is_not_kind_and_name(files: Files) -> None:
    doc = files["shop/glossary/aov.yaml"]
    doc["status"] = "deprecated"
    doc["deprecation"] = {"reason": "Merged.", "replaced_by": "aov2"}


# --- V15 ---


def v15_default_grain_not_offered(files: Files) -> None:
    _metric_doc(files, "inventory_balance")["default_grain"] = "day"


def v15_balance_metric_offers_day(files: Files) -> None:
    _metric_doc(files, "inventory_balance")["time_grains"] = ["day", "month"]
    _metric_doc(files, "inventory_balance")["default_grain"] = "month"


def v15_balance_metric_with_default_grains(files: Files) -> None:
    del _metric_doc(files, "inventory_balance")["time_grains"]
    del _metric_doc(files, "inventory_balance")["default_grain"]


def v15_ratio_over_balance_offers_week(files: Files) -> None:
    # A ratio of a balance by a flow still sees a balance among its inputs.
    doc = _metric_doc(files, "revenue_per_order")
    doc["denominator"] = "inventory_balance"
    doc["time_grains"] = ["week", "month"]


# --- V16 ---


def v16_time_dimension_is_personal(files: Files) -> None:
    _named(_entity(files, "customer")["measures"], "signups")["time_dimension"] = "birth_year"


# --- V17 ---


def v17_personal_dimension_lists_values(files: Files) -> None:
    dim = _named(_entity(files, "customer")["dimensions"], "email")
    dim["values"] = [{"value": "a@example.com"}]


# --- V18 ---


def v18_measure_rule_not_on_entity(files: Files) -> None:
    _named(_entity(files, "order")["measures"], "orders")["rule"] = "late_order"


def v18_metric_rule_entity_not_reachable(files: Files) -> None:
    # The measure is on customer, which reaches region but not order, where the rule lives.
    _metric_doc(files, "order_count")["measure"] = "customer.signups"


def v18_metric_rule_does_not_exist(files: Files) -> None:
    _metric_doc(files, "order_count")["filters"] = [{"rule": "order.late_order"}]


def v18_rule_reads_personal_column(files: Files) -> None:
    _entity(files, "customer")["rules"] = [
        {
            "name": "gmail_user",
            "description": "A customer whose address ends in gmail.",
            "expr": "email LIKE '%gmail.com'",
        }
    ]


# --- V20 ---


def v20_time_copy_of_a_masked_column(files: Files) -> None:
    _entity(files, "customer")["dimensions"].append(
        _dim("birth_copy", "time", expr="birth_date", sensitivity="internal")
    )


def v20_categorical_copy_inside_a_function(files: Files) -> None:
    dims = _entity(files, "customer")["dimensions"]
    dims.append(_dim("shouted_email", "categorical", expr="UPPER(email)", sensitivity="internal"))


# (rule, fixture name) -> mutation. Each must make the validator report exactly that code.
FIXTURES: dict[str, list[Mutation]] = {
    "V01": [v01_metric_without_id_synonym, v01_unknown_key],
    "V02": [
        v02_file_name_differs_from_name,
        v02_wrong_domain_folder,
        v02_official_file_with_draft_status,
        v02_proposal_file_with_approved_status,
    ],
    "V03": [
        v03_same_metric_name_in_two_files,
        v03_duplicate_dimension_name,
        v03_duplicate_measure_name,
        v03_duplicate_relationship_name,
        v03_dimension_name_with_a_double_underscore,
        v03_entity_name_with_a_double_underscore,
    ],
    "V04": [v04_domain_does_not_exist],
    "V05": [
        v05_dimension_with_aggregate,
        v05_measure_with_subquery,
        v05_rule_with_window_function,
        v05_expression_does_not_parse,
        v05_two_expressions_in_one,
    ],
    "V06": [v06_time_dimension_is_not_a_time_dimension, v06_time_dimension_does_not_exist],
    "V07": [v07_relationship_to_missing_entity],
    "V08": [
        v08_simple_measure_does_not_resolve,
        v08_simple_measure_entity_does_not_exist,
        v08_ratio_input_is_not_a_metric,
        v08_ratio_cycle,
        v08_derived_expression_uses_name_outside_inputs,
        v08_derived_expression_uses_a_function,
    ],
    "V09": [
        v09_chain_does_not_start_at_the_measure_entity,
        v09_chain_is_not_connected,
        v09_chain_ends_at_another_entity,
        v09_relationship_does_not_exist,
    ],
    "V10": [
        v10_filter_on_personal_dimension,
        v10_filter_value_of_wrong_type,
        v10_filter_value_not_in_values,
        v10_filter_dimension_not_reachable,
        v10_filter_on_unknown_dimension,
        v10_date_filter_with_text,
    ],
    "V11": [v11_truncate_date_on_categorical, v11_bucket_on_time, v11_redact_on_numeric],
    "V12": [
        v12_sum_over_personal_column,
        v12_filter_on_personal_column_of_a_sum,
        v12_count_over_an_expression_of_a_personal_column,
        v12_count_with_a_filter_on_a_personal_column,
    ],
    "V13": [
        v13_approved_metric_depends_on_draft_metric,
        v13_in_review_metric_depends_on_draft_entity,
        v13_approved_metric_depends_on_deprecated_metric,
    ],
    "V14": [
        v14_deprecated_without_reason,
        v14_replaced_by_does_not_resolve,
        v14_replaced_by_is_not_kind_and_name,
    ],
    "V15": [
        v15_default_grain_not_offered,
        v15_balance_metric_offers_day,
        v15_balance_metric_with_default_grains,
        v15_ratio_over_balance_offers_week,
    ],
    "V16": [v16_time_dimension_is_personal],
    "V17": [v17_personal_dimension_lists_values],
    "V18": [
        v18_measure_rule_not_on_entity,
        v18_metric_rule_entity_not_reachable,
        v18_metric_rule_does_not_exist,
        v18_rule_reads_personal_column,
    ],
    "V20": [v20_time_copy_of_a_masked_column, v20_categorical_copy_inside_a_function],
}
