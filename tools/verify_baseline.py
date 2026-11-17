"""Verify unchanged M01 production code against the frozen baseline."""
import hashlib
import json
from pathlib import Path
root=Path(__file__).resolve().parents[1]
baseline=json.loads((root/'M01_BASELINE.json').read_text())
changed=[name for name,h in baseline['m01_source_sha256'].items() if not (root/name).is_file() or hashlib.sha256((root/name).read_bytes()).hexdigest()!=h]
print(json.dumps({'baseline_id':baseline['baseline_id'],'checked':len(baseline['m01_source_sha256']),'changes':changed}))
raise SystemExit(bool(changed))
