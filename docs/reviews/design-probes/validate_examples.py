import glob, json, sys
from pathlib import Path
import yaml, jsonschema
from jsonschema import Draft202012Validator
root = Path(__file__).resolve().parents[3] / "semantic/schema"
schema = json.loads((root / "makna.schema.json").read_text())
Draft202012Validator.check_schema(schema)
print("schema itself: valid Draft 2020-12 schema; jsonschema", jsonschema.__version__)
v = Draft202012Validator(schema)
files = sorted(glob.glob(str(root / "examples/**/*.yaml"), recursive=True))
passed = 0
for f in files:
    doc = yaml.safe_load(Path(f).read_text())
    errs = sorted(v.iter_errors(doc), key=lambda e: list(e.absolute_path))
    # also validate directly against the kind's own definition
    sub = Draft202012Validator({"$ref": f"#/$defs/{doc['kind']}", "$defs": schema["$defs"]})
    errs2 = list(sub.iter_errors(doc))
    rel = Path(f).relative_to(root)
    if errs or errs2:
        print(f"FAIL {rel}")
        for e in errs + errs2:
            print(f"   at {'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}")
    else:
        passed += 1
        print(f"PASS {rel} (kind={doc['kind']})")
print(f"{passed}/{len(files)} files pass")
