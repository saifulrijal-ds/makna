import json, copy
from pathlib import Path
import yaml
from jsonschema import Draft202012Validator
root = Path(__file__).resolve().parents[3] / "semantic/schema"
v = Draft202012Validator(json.loads((root / "makna.schema.json").read_text()))
L = lambda p: yaml.safe_load((root / "examples/lending" / p).read_text())
def check(label, doc):
    errs = list(v.iter_errors(doc))
    print(f"{'REJECTED' if errs else 'ACCEPTED'}  {label}" + (f"  -> {errs[0].message[:110]}" if errs else ""))
m = L("metrics/collected_amount.yaml"); r = L("metrics/npf_ratio.yaml"); e = L("entities/customer.yaml"); g = L("glossary/npf.yaml"); d = L("domain.yaml")
x = copy.deepcopy(m); x["unknown_key"] = 1; check("metric with unknown key", x)
x = copy.deepcopy(r); x["measure"] = "contract.disbursed_principal"; check("ratio with measure", x)
x = copy.deepcopy(m); x["status"] = "deprecated"; check("deprecated metric without deprecation", x)
x = copy.deepcopy(e); x["status"] = "deprecated"; check("deprecated ENTITY without deprecation (V14 says every deprecated object needs a reason)", x)
x = copy.deepcopy(g); x["status"] = "deprecated"; check("deprecated GLOSSARY TERM without deprecation", x)
x = copy.deepcopy(d); x.pop("synonyms"); check("domain with no synonyms (SEM-2: every object has synonyms)", x)
x = copy.deepcopy(m); x.pop("synonyms"); check("metric with no synonyms (SEM-2)", x)
x = copy.deepcopy(e); x["dimensions"][-1]["values"] = [{"value": "9912345678901234", "description": "Budi Santoso"}]; check("personal dimension national_id listing raw values", x)
x = copy.deepcopy(e); x["dimensions"][-1]["masking"] = {"method": "partial", "keep_last": 8}; check("national_id masked with partial keep_last 8 (half the NIK)", x)
x = copy.deepcopy(e); x["dimensions"][4]["masking"] = {"method": "partial", "keep_last": 4}; check("time dimension birth_year with partial masking (V11 mismatch)", x)
x = copy.deepcopy(e); x["measures"].append({"name": "oldest_birth", "agg": "min", "expr": "birth_date", "time_dimension": "registered_date", "description": "Earliest birth date of customers."}); check("measure MIN(birth_date) over a personal column (V12)", x)
x = copy.deepcopy(e); x["measures"].append({"name": "by_birth", "agg": "count", "time_dimension": "birth_year", "description": "Customers counted by date of birth."}); check("measure whose time_dimension is the personal birth_year", x)
x = copy.deepcopy(m); x["filters"] = [{"dimension": "branch.region_name", "op": "in", "value": "Jawa Barat"}]; check("filter op in with scalar value", x)
x = copy.deepcopy(m); x["filters"] = [{"dimension": "customer.national_id", "op": "=", "value": "99"}]; check("metric filter on personal dimension (V10)", x)
x = copy.deepcopy(m); x["description"] = "SELECT * FROM lending.core.customers WHERE 1=1"; check("description containing SQL ('No SQL')", x)
x = copy.deepcopy(e); x["dimensions"][0]["values"] = [{"value": "mass"}]; x["dimensions"][3]["values"]=[{"value":"2020-01-01"}]; check("values on a time dimension", x)
x = copy.deepcopy(m); x["time_grains"] = ["month"]; x["default_grain"] = "day"; check("default_grain not in time_grains (V15)", x)
x = copy.deepcopy(m); x["join_paths"] = {"branch": ["payment.contract", "contract.servicing_branch"], "customer": []}; check("join_paths with empty chain", x)
x = copy.deepcopy(e); x["relationships"][0]["type"] = "one_to_one"; check("relationship type one_to_one (not described in ARCHITECTURE 5.1)", x)
