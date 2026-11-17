"""Transparent local package-parse evidence for Case 01 T10-R064 only.

No LLM calls, repairs, financial/content judgments, or edits to the base result.
"""
from __future__ import annotations

import argparse
from importlib.metadata import version
import json
from pathlib import Path
import shutil

from . import evaluation as ev
from . import eval_completion as completion


RID = "P02v3-T10-R064"
FILES = ("IC_prereading_pack_vF.pptx", "invitation letter.docx", "weekly_status_note_2026-07-17.docx")
DESCRIPTION = "IC_prereading_pack_vF.pptx、invitation letter.docx、weekly_status_note_2026-07-17.docx任一在作答目录不存在，或存在但对应库无法读出内容：pptx用python-pptx打开报错/读出零页，docx用python-docx打开报错/读出零段落，即命中。仅判存在及对应库能否打开/读出，不判内容、排版或质量。"
BACKEND = "deterministic_local_package_parse"


def parse_artifacts(paths: dict[str, Path]) -> list[dict]:
    from docx import Document
    from pptx import Presentation
    packages = {".docx": ("python-docx",version("python-docx")), ".pptx": ("python-pptx",version("python-pptx"))}
    records = []
    for name in FILES:
        path = Path(paths[name]); package, package_version = packages[path.suffix.lower()]
        row = {"artifact":name,"library":package,"library_version":package_version,"exists":path.is_file(),
               "original_sha256":None,"parse_succeeded":False,"criterion_violation":False}
        if not path.exists():
            row.update(criterion_violation=True,observation="required_artifact_missing"); records.append(row);continue
        if path.is_symlink() or not path.is_file():raise ev.EvaluationError("Nonregular frozen artifact")
        digest=ev._hash(path);row["original_sha256"]=digest
        try:
            if path.suffix.lower()==".pptx":
                presentation=Presentation(path);count=len(presentation.slides)
                row.update(parse_succeeded=True,slide_count=count,criterion_violation=count==0)
            else:
                document=Document(path);paragraphs=len(document.paragraphs)
                row.update(parse_succeeded=True,paragraph_count=paragraphs,table_count=len(document.tables),criterion_violation=paragraphs==0)
                # The exact criterion says zero paragraphs. A table-only file
                # does not satisfy that explicit condition merely by existing.
        except Exception as exc:
            row.update(parse_error_type=type(exc).__name__,criterion_violation=True,
                       observation="specified_library_failed_to_open_or_read")
        if ev._hash(path)!=digest:raise ev.EvaluationError("Artifact changed during mechanical inspection")
        row["original_hash_unchanged"]=True;records.append(row)
    return records


def mechanical_verdict(item: dict, observations: list[dict]) -> dict:
    if (item.get("id")!=RID or item.get("description")!=DESCRIPTION or item.get("artifact_scope")!=list(FILES)
            or item.get("hurdle") is not True or item.get("weight")!=0):
        raise ev.EvaluationError("Unsupported criterion; no generic semantic grading is implemented")
    if [r["artifact"] for r in observations]!=list(FILES):raise ev.EvaluationError("Incomplete mechanical observations")
    hit=any(row["criterion_violation"] for row in observations)
    evidence=[]
    for row in observations:
        details={k:row[k] for k in ["exists","library","library_version","original_sha256","parse_succeeded",
            "slide_count","paragraph_count","table_count","parse_error_type","original_hash_unchanged"] if k in row}
        evidence.append("mechanical_observations.json; submission/"+row["artifact"]+": "+json.dumps(details,ensure_ascii=False))
    return {"rubric_id":RID,"status":"graded","hit":hit,"evidence":evidence,"missing_information":[],
        "reason":("Actual named-library loading detected a missing artifact, parser failure, zero PPTX slides or zero DOCX paragraphs. " if hit else
                  "Actual python-pptx loading read at least one slide, and actual python-docx loading read at least one body paragraph in each DOCX. ")+"Only the criterion's mechanical existence/readability condition was tested; content, layout and financial quality were not judged.",
        "finding":"violation_present" if hit else "violation_absent"}


def complete_mechanical_unit(case_root: Path, run_dir: Path, base_dir: Path, output_dir: Path) -> dict:
    case_root,run_dir,base_dir,output_dir=[Path(p).resolve() for p in (case_root,run_dir,base_dir,output_dir)]
    if output_dir==base_dir or output_dir.is_relative_to(base_dir) or base_dir.is_relative_to(output_dir):raise ev.EvaluationError("Output must be separate from base unit")
    if output_dir.is_relative_to(run_dir/"workspace"):raise ev.EvaluationError("Output cannot be in Candidate workspace")
    if output_dir.exists() and any(output_dir.iterdir()):raise ev.EvaluationError("Output directory must be new")
    base=completion._load_base(case_root,base_dir)
    if base["unit"]!="T10" or {i["id"] for i in base["pending"]}!={RID}:raise ev.EvaluationError("Only the single pending T10-R064 is supported")
    record,frozen=ev._freeze(run_dir,"T10")
    paths={name:ev._safe(frozen,"working/checkpoints/T10/"+name) for name in FILES}
    for name,path in paths.items():
        logical="submission/"+name;view=ev._safe(base_dir/"evidence",logical)
        if path.exists()!=view.exists() or (path.exists() and ev._hash(path)!=ev._hash(view)):
            raise ev.EvaluationError("Base view and frozen artifact differ")
    observations=parse_artifacts(paths);item=base["pending"][0]
    verdict=mechanical_verdict(item,observations);official=base["official"];ev.validate_row(official,verdict,item)
    output_dir.mkdir(parents=True,exist_ok=True)
    base_files={name:ev._hash(base_dir/name) for name in ["evaluation_result.json","verdicts.json","rubric.json","adapter_protocol.json","evidence/evaluation_manifest.json"]}
    provenance={"base_files":base_files,"fixed_graded_row_sha256":base["fixed_hashes"],
        "mechanical_module_sha256":ev._hash(Path(__file__)),"completion_module_sha256":ev._hash(Path(completion.__file__)),
        "frozen_v4_protocol":base["protocol"],"method":BACKEND,"supported_criterion_description":DESCRIPTION,
        "freeze_record_sha256":ev._hash(run_dir/"freezes/workspace/T10/freeze.json")}
    ev._dump(output_dir/"base_provenance.json",provenance)
    ev._dump(output_dir/"mechanical_observations.json",{"backend":BACKEND,"criterion":RID,"observations":observations,
        "scope":"exists + exact named-library open/slide-or-body-paragraph count only; tables recorded but do not override zero-paragraph condition"})
    ev._dump(output_dir/"incremental_verdicts.json",[verdict]);shutil.copyfile(base_dir/"rubric.json",output_dir/"rubric.json")
    merged=completion.merge_verdicts(base["rubric"]["rubrics"],base["rows"],base["raw"],[verdict],output_dir/"verdicts.json")
    score=ev.finalize_score(official,base["rubric"]["rubrics"],merged)
    contract=ev._json(base["directory"]/"runtime_contract.json")
    try:official["verify"].validate_incompatible_hits(score,contract.get("incompatible_hits",[]),output_dir/"score_details.json")
    except RuntimeError:
        if score.get("status")!="judge_error":raise
    ev._dump(output_dir/"score_details.json",score)
    result={"evaluation_unit":"T10","mode":BACKEND,"protocol_version":completion.PROTOCOL,"judge_backend":BACKEND,
        "official_harbor_run":False,"status":score["status"],"local_score":score.get("score"),"score_raw":score.get("score_raw"),
        "base_evaluation_dir":str(base_dir),"base_score_is_not_replaced":True,"base_status":base["result"]["status"],
        "base_local_score":base["result"].get("local_score"),"base_evaluation_result_sha256":base_files["evaluation_result.json"],
        "base_verdicts_sha256":base_files["verdicts.json"],"resolved_verdicts_sha256":ev._hash(output_dir/"verdicts.json"),
        "base_graded_rows_unchanged":True,"fixed_graded_rows_preserved":True,"fixed_graded_count":len(base["fixed_hashes"]),
        "eligible_ids":[RID],"supplemental_verdict_count":1,"rubric_count":len(base["rubric"]["rubrics"]),"verdict_count":len(merged),
        "hurdles":score.get("hurdles",[]),"missing_verdicts":score.get("missing_verdicts",[]),
        "model_calls":0,"judge_metrics":{"model_calls":0,"estimated_cost_rmb":0,"backend":BACKEND}}
    ev._dump(output_dir/"adapter_protocol.json",{"protocol_version":completion.PROTOCOL,"mode":BACKEND,"provenance":provenance,"model_calls":0})
    ev._dump(output_dir/"evaluation_result.json",result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ["case","run-dir","base-eval","output"]:parser.add_argument("--"+name,required=True,type=Path)
    args=parser.parse_args()
    print(json.dumps(complete_mechanical_unit(args.case,args.run_dir,args.base_eval,args.output),ensure_ascii=False,indent=2))


if __name__=="__main__":main()
