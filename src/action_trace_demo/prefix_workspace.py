"""Evaluator-side construction of a released historical checkpoint for repair trials.

The DSH subprocess sees only the new operator workspace. Historical proposals of
the target stage, later stages, judge inputs and private responder state are
never mounted. Drafts are the candidate's own signed files for the current
stage, explicitly provided for a repair experiment.
"""
from pathlib import Path
import shutil
import sqlite3

from .dsh_bridge import open_stage, write_json
from .financial_projection import bind_current_model
from .guarded_artifacts import ArtifactOperator
from .task_obligations import register_obligations
from .temporal_dag import TemporalDAG
from .tools import digest


def _events(db, kind, stage):
    import json
    return [json.loads(payload) for payload, in db.execute(
        "SELECT payload FROM event WHERE kind=? AND json_extract(payload,'$.stage')=? ORDER BY seq",
        (kind, stage))]


def prepare_checkpoint(base: Path, destination: Path, stage: int, *, review_policy="ledger_graph"):
    """Return context, DSH prompt inputs and requested-path -> historical draft."""
    if stage not in (6, 8, 13):
        raise ValueError("Choose T06, T08 or T13 for this targeted experiment")
    base = base.resolve()
    destination = destination.resolve()
    if destination == base or destination.is_relative_to(base) or destination.exists():
        raise ValueError("Choose a new directory outside the historical run")
    root = destination / "operator"
    dag = TemporalDAG(root)
    old = base / "operator"
    readonly = (old / "temporal_dag.sqlite3").as_uri() + "?mode=ro&immutable=1"
    with sqlite3.connect(readonly, uri=True) as original, sqlite3.connect(dag.path) as copy:
        opening, = original.execute("SELECT seq FROM event WHERE kind='stage' AND json_extract(payload,'$.stage')=?",
                                    (stage,)).fetchone()
        for table, predicate in (("event","seq"), ("released","seq"), ("dependency","child")):
            rows = original.execute(f"SELECT * FROM {table} WHERE {predicate} < ?", (opening,))
            count = len(rows.description)
            copy.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in range(count))})", rows)
        stage_info, = _events(original, "stage", stage)
        public = [p for p in _events(original, "source", stage)
                  if p["locator"].startswith(("source/", "instructions/")) and
                  not p["source_key"].startswith(("artifact-observation:", "native:"))]
    art = ArtifactOperator(root, dag)
    heads = {}
    # Historical read/version requests can inspect any original copy in the
    # actual prefix, so carry its content-addressed blobs as well as the heads.
    for n in dag.nodes():
        p = n["payload"]
        h = p.get("sha256")
        if h and p.get("path") and n["kind"] in {"artifact_check","artifact_commit_intent","artifact_version"}:
            suffix = Path(p["path"]).suffix.lower()
            if not (art.store / (h+suffix)).exists():
                original_blob = old / ".artifact_versions" / (h+suffix)
                if digest(original_blob) != h:
                    raise ValueError("Historical native snapshot changed")
                shutil.copyfile(original_blob, art.store / (h+suffix))
        if n["kind"] == "artifact_version":
            heads[p["path"]] = n
    for path, node in heads.items():
        h = node["payload"]["sha256"]
        if h:
            dest = art.materialized(path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(art._blob(path, h), dest)
    sources = dag.nodes("source")
    for previous in dag.nodes("stage"):
        sp = previous["payload"]
        ids = [n["id"] for n in sources if n["payload"]["stage"] == sp["stage"]
               and n["payload"]["locator"].startswith(("instructions/", "source/"))]
        register_obligations(dag, previous, ids, sp["expected"])
    ctx = open_stage(root, stage, stage_info["instruction"], stage_info["expected"],
                     [(p["locator"],p["text"]) for p in public], self_review=True, review_policy=review_policy)
    seen = {n["payload"]["locator"]:n for n in dag.nodes("source")}
    freeze = base / "freezes/workspace" / f"T{stage:02}"
    import json
    receipt = json.loads((freeze / "freeze.json").read_text())
    for p in public:
        path = p["locator"]
        if not path.startswith("source/") or Path(path).suffix.casefold() not in {".xlsx",".docx",".pptx",".pdf",".md",".txt"}:
            continue
        source = base / "workspace" / path
        if digest(source) != receipt["published_source_hashes"][path]:
            raise ValueError("Historical visible source changed")
        art.seed(path, source, purpose="released source snapshot", description="Original available to this stage",
                 parents=[seen[path]["id"]], stage=stage)
    bind_current_model(root, ctx)
    drafts = {}
    preview = {}
    for path in ctx["expected"]:
        file = freeze / "evidence" / path
        h = receipt["evidence_files"][path]
        if digest(file) != h:
            raise ValueError("Historical draft does not match its frozen version")
        native_name = "drafts/" + path
        art.seed(native_name, file, purpose="candidate draft for inspection",
                 description="Previously submitted candidate document supplied for scoped repair.", stage=stage)
        drafts[path] = native_name
        preview[path] = h
    write_json(destination / "prefix.json", {"stage":stage, "last_historical_event":opening-1,
               "added_requirement_history":True, "source_freeze_sha256":digest(freeze/"freeze.json"),
               "historical_draft_sha256":preview,"policy":review_policy})
    info = {**stage_info,"stage_id":f"T{stage:02}","expected_outputs":ctx["expected"]}
    return ctx, info, drafts
