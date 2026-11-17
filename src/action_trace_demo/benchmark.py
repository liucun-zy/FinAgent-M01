"""Candidate-only DeliverBench stage release for local research runs.

This module reads public task metadata and currently released instructions only.
It never loads the official contract, rubrics, authored references, scorer, or
simulator internals. Local evidence freezes are not DeliverBench judgments.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import tomllib
from pathlib import Path
from typing import Any, Callable


class CaseProtocolError(RuntimeError):
    """The research run cannot advance while preserving its evidence protocol."""


class DynamicResponseRequired(CaseProtocolError):
    """T11 requires a real isolated Responder before its inputs can be released."""


class FrozenEvidenceChanged(CaseProtocolError):
    """Previously frozen evidence no longer has its recorded contents."""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _files(root: Path) -> list[Path]:
    if root.is_symlink():
        raise CaseProtocolError("Symbolic links are not allowed in case material or artifacts")
    if not root.exists():
        return []
    if root.is_symlink() or not root.is_dir():
        raise CaseProtocolError(f"Expected a regular directory: {root.name}")
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise CaseProtocolError("Symbolic links are not allowed in case material or artifacts")
        if path.is_file():
            found.append(path)
        elif not path.is_dir():
            raise CaseProtocolError("Only regular files and directories are allowed")
    return found


def _inside(root: Path, relative: str) -> Path:
    """Resolve a relative path without allowing traversal or symlink components."""
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise CaseProtocolError("Path must stay inside its declared directory")
    candidate = root / rel
    cursor = root
    if cursor.is_symlink():
        raise CaseProtocolError("Directory must not be a symbolic link")
    for part in rel.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise CaseProtocolError("Symbolic links are not allowed")
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise CaseProtocolError("Path escapes its declared directory") from exc
    return candidate


class DeliverBenchCase:
    """Release a case sequentially into a separate Candidate workspace.

    ``working/current`` is a logical alias for the current checkpoint, not a
    symlink. The calling tool layer must resolve it and enforce frozen paths as
    read-only; this module checks their hashes before every protocol transition.
    No arbitrary setup script is executed. T11 is blocked unless a real isolated
    Responder bridge is supplied; no fixture or placeholder response is accepted.
    """

    def __init__(
        self, case_root: Path, workspace: Path,
        *, responder: Callable[[Path, Path], Path] | None = None,
    ):
        self.case_root = Path(case_root).resolve()
        self.task_root = _inside(self.case_root, "task")
        self.workspace = Path(workspace).absolute()
        if self.workspace.is_symlink():
            raise CaseProtocolError("Candidate workspace must not be a symbolic link")
        self.workspace = self.workspace.resolve()
        self.responder = responder
        if self.workspace == self.case_root or self.case_root in self.workspace.parents:
            raise CaseProtocolError("Candidate workspace must be outside the benchmark case")
        metadata_path = self.task_root / "deliverbench.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.steps = metadata.get("steps", [])
        if not self.steps or any(not isinstance(x, str) or not re.fullmatch(r"T\d{2}", x) for x in self.steps):
            raise CaseProtocolError("Invalid public stage identifier list")
        if len(set(self.steps)) != len(self.steps):
            raise CaseProtocolError("Stage identifiers must be unique")
        self.kickoff = metadata.get("kickoff", {}).get("instructions", ["instruction.md"])
        if not isinstance(self.kickoff, list) or any(not isinstance(x, str) for x in self.kickoff):
            raise CaseProtocolError("Invalid opening instruction metadata")
        self.metadata_sha256 = _sha(metadata_path)
        self.state_path = self.workspace.parent / f".{self.workspace.name}.case_state.json"
        self.freeze_root = self.workspace.parent / "freezes" / self.workspace.name
        if self.state_path.exists():
            self._state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if self._state.get("case_root") != str(self.case_root) or self._state.get("metadata_sha256") != self.metadata_sha256:
                raise CaseProtocolError("Existing run belongs to a different case or metadata revision")
        else:
            self._state = {
                "schema_version": "0.1", "mode": "local_research_candidate_only",
                "official_evaluation": False, "case_root": str(self.case_root),
                "metadata_sha256": self.metadata_sha256, "initialized": False,
                "current_stage": 0, "frozen_stages": [], "freeze_record_hashes": {},
                "published_sources": {},
            }

    def _save(self) -> None:
        _write_json(self.state_path, self._state)

    def _verify_sources(self) -> None:
        actual_paths = {p.relative_to(self.workspace).as_posix() for p in _files(self.workspace / "source")}
        if actual_paths != set(self._state["published_sources"]):
            raise FrozenEvidenceChanged("Published source file set changed")
        for relative, digest in self._state["published_sources"].items():
            path = _inside(self.workspace, relative)
            if not path.is_file() or _sha(path) != digest:
                raise FrozenEvidenceChanged(f"Published source changed: {relative}")

    def _verify_freezes(self) -> None:
        for stage_id in self._state["frozen_stages"]:
            record_path = self.freeze_root / stage_id / "freeze.json"
            if not record_path.is_file():
                raise FrozenEvidenceChanged(f"Freeze record is missing: {stage_id}")
            if _sha(record_path) != self._state.get("freeze_record_hashes", {}).get(stage_id):
                raise FrozenEvidenceChanged(f"Freeze record changed: {stage_id}")
            record = json.loads(record_path.read_text(encoding="utf-8"))
            checkpoint = _inside(self.workspace, f"working/checkpoints/{stage_id}")
            current = {str(p.relative_to(self.workspace)): _sha(p) for p in _files(checkpoint)}
            if current != record["checkpoint_files"]:
                raise FrozenEvidenceChanged(f"Frozen checkpoint changed: {stage_id}")
            evidence_root = self.freeze_root / stage_id / "evidence"
            evidence = {str(p.relative_to(evidence_root)): _sha(p) for p in _files(evidence_root)}
            if evidence != record["evidence_files"]:
                raise FrozenEvidenceChanged(f"Frozen evidence copy changed: {stage_id}")

    def _verify_integrity(self) -> None:
        self._verify_sources()
        self._verify_freezes()

    def _publish(self, source_root: Path) -> list[str]:
        """Preflight the complete release before writing any source file."""
        return self._publish_many([source_root])

    def _publish_many(self, source_roots: list[Path]) -> list[str]:
        """Atomically preflight static and public dynamic inputs together."""
        pending: list[tuple[Path, Path, str, str]] = []
        planned: dict[str, str] = {}
        for source_root in source_roots:
            for source in _files(source_root):
                relative = "source/" + source.relative_to(source_root).as_posix()
                destination = _inside(self.workspace, relative)
                digest = _sha(source)
                if destination.exists() and (not destination.is_file() or _sha(destination) != digest):
                    raise CaseProtocolError(f"Source release would replace existing content: {relative}")
                old = self._state["published_sources"].get(relative)
                if old is not None and old != digest:
                    raise CaseProtocolError(f"Source release conflicts with published source: {relative}")
                if relative in planned:
                    if planned[relative] != digest:
                        raise CaseProtocolError(f"Static and dynamic source releases conflict: {relative}")
                    continue
                planned[relative] = digest
                pending.append((source, destination, relative, digest))
        for source, destination, relative, digest in pending:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                shutil.copyfile(source, destination)
            self._state["published_sources"][relative] = digest
        return [item[2] for item in pending]

    def initialize(self) -> dict:
        self._verify_integrity()
        sections = []
        for relative in self.kickoff:
            if relative != "instruction.md" and not relative.startswith("environment/data/source/"):
                raise CaseProtocolError("Opening instruction is outside the Candidate opening view")
            path = _inside(self.task_root, relative)
            if not path.is_file():
                raise CaseProtocolError(f"Opening instruction is missing: {relative}")
            sections.append(path.read_text(encoding="utf-8"))
        if not self._state["initialized"]:
            if self.workspace.exists() and any(self.workspace.iterdir()):
                raise CaseProtocolError("A fresh run requires an empty Candidate workspace")
            self.workspace.mkdir(parents=True, exist_ok=True)
            released = self._publish(_inside(self.task_root, "environment/data/source"))
            (self.workspace / "working" / "checkpoints").mkdir(parents=True, exist_ok=True)
            (self.workspace / "deliverables" / "final").mkdir(parents=True, exist_ok=True)
            self._state["initialized"] = True
            self._save()
        else:
            released = sorted(self._state["published_sources"])
        return {
            "stage": 0, "stage_id": "OPENING", "instruction": "\n\n".join(sections),
            "released_files": released, "current_dir": None,
            "official_evaluation": False, "mode": self._state["mode"],
        }

    def _expected_outputs(self, stage_id: str) -> list[str]:
        path = self.task_root / "task.toml"
        if not path.is_file():
            return []
        # This is public output-path metadata only; do not load verifier contracts.
        metadata = tomllib.loads(path.read_text(encoding="utf-8"))
        for step in metadata.get("steps", []):
            if step.get("name") == stage_id:
                raw = step.get("verifier", {}).get("env", {}).get("TASK_OUTPUT_PATHS", "")
                if not isinstance(raw, str):
                    raise CaseProtocolError("Invalid public output-path metadata")
                results = []
                for item in raw.split(":"):
                    if not item:
                        continue
                    if not item.startswith("/workspace/"):
                        raise CaseProtocolError("Output metadata is outside Candidate workspace")
                    relative = item[len("/workspace/"):]
                    _inside(self.workspace, relative)
                    results.append(relative)
                return results
        return []

    def release(self, stage: int) -> dict:
        if not self._state["initialized"]:
            raise CaseProtocolError("Initialize the opening before releasing a stage")
        if not isinstance(stage, int) or isinstance(stage, bool) or not 1 <= stage <= len(self.steps):
            raise CaseProtocolError("Stage number is outside the public stage list")
        self._verify_integrity()
        current = self._state["current_stage"]
        if stage != current + 1:
            raise CaseProtocolError("Stages must be released once, in increasing order")
        if current:
            previous = self.steps[current - 1]
            if previous not in self._state["frozen_stages"]:
                raise CaseProtocolError("Freeze the current submission before releasing the next stage")
            record = json.loads((self.freeze_root / previous / "freeze.json").read_text(encoding="utf-8"))
            if not record["submission_complete"]:
                raise CaseProtocolError("Cannot advance past an incomplete frozen submission")
        stage_id = self.steps[stage - 1]
        if stage_id == "T11" and self.responder is None:
            raise DynamicResponseRequired(
                "T11 requires a real isolated P02_LENDER Responder output. "
                "This local runner does not implement it; no T11 input was released."
            )
        stage_root = _inside(self.task_root, f"steps/{stage_id}")
        public_response = None
        if stage_id == "T11":
            try:
                public_response = Path(self.responder(self.workspace, stage_root))  # type: ignore[misc]
                if public_response.is_symlink() or not public_response.is_dir():
                    raise CaseProtocolError("Invalid public response directory")
                if public_response.resolve().is_relative_to(self.workspace):
                    raise CaseProtocolError("Responder must stage its output outside Candidate workspace")
                if not _files(public_response):
                    raise CaseProtocolError("Responder did not generate a public file")
            except Exception:
                raise DynamicResponseRequired("T11 real Responder failed; no T11 materials were released") from None
        instruction_path = _inside(stage_root, "instruction.md")
        if not instruction_path.is_file():
            raise CaseProtocolError(f"Current instruction is missing: {stage_id}")
        instruction = instruction_path.read_text(encoding="utf-8")
        expected = self._expected_outputs(stage_id)
        current_dir = _inside(self.workspace, f"working/checkpoints/{stage_id}")
        if current_dir.exists():
            raise CaseProtocolError("Current checkpoint must start as a new empty directory")
        release_roots = [_inside(stage_root, "workdir/release_materials")]
        if public_response is not None:
            release_roots.append(public_response)
        released = self._publish_many(release_roots)
        current_dir.mkdir(parents=True)
        self._state["current_stage"] = stage
        self._save()
        return {
            "stage": stage, "stage_id": stage_id, "instruction": instruction,
            "released_files": released,
            "current_dir": current_dir.relative_to(self.workspace).as_posix(),
            "expected_outputs": expected, "official_evaluation": False,
            "mode": self._state["mode"],
        }

    def _output_exists(self, relative: str, stage_id: str) -> bool:
        if relative.startswith("working/current/"):
            relative = "working/checkpoints/" + stage_id + "/" + relative[len("working/current/"):]
        return _inside(self.workspace, relative).is_file()

    def freeze(self, stage: int) -> dict:
        if not isinstance(stage, int) or isinstance(stage, bool) or stage != self._state["current_stage"] or stage < 1:
            raise CaseProtocolError("Only the current released stage can be frozen")
        self._verify_integrity()
        stage_id = self.steps[stage - 1]
        record_path = self.freeze_root / stage_id / "freeze.json"
        if stage_id in self._state["frozen_stages"]:
            return json.loads(record_path.read_text(encoding="utf-8"))
        checkpoint = _inside(self.workspace, f"working/checkpoints/{stage_id}")
        final_root = _inside(self.workspace, "deliverables/final")
        checkpoint_files = {p.relative_to(self.workspace).as_posix(): _sha(p) for p in _files(checkpoint)}
        artifacts = _files(checkpoint) + _files(final_root)
        expected = self._expected_outputs(stage_id)
        missing = [p for p in expected if not self._output_exists(p, stage_id)]
        evidence_root = self.freeze_root / stage_id / "evidence"
        if evidence_root.exists():
            raise CaseProtocolError("A partial freeze already exists; do not overwrite evidence")
        evidence_root.mkdir(parents=True)
        evidence_files = {}
        for path in artifacts:
            relative = path.relative_to(self.workspace).as_posix()
            destination = _inside(evidence_root, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            evidence_files[relative] = _sha(destination)
        record = {
            "schema_version": "0.1", "stage": stage, "stage_id": stage_id,
            "mode": "local_research_freeze", "official_evaluation": False,
            "submission_complete": not missing,
            "missing_expected_outputs": missing, "expected_outputs": expected,
            "checkpoint_files": checkpoint_files, "evidence_files": evidence_files,
            "published_source_hashes": dict(self._state["published_sources"]),
        }
        _write_json(record_path, record)
        self._state["frozen_stages"].append(stage_id)
        self._state["freeze_record_hashes"][stage_id] = _sha(record_path)
        self._save()
        return record

    def status(self) -> dict:
        self._verify_integrity()
        return {
            "mode": self._state["mode"], "official_evaluation": False,
            "initialized": self._state["initialized"],
            "current_stage": self._state["current_stage"],
            "current_stage_id": self.steps[self._state["current_stage"] - 1] if self._state["current_stage"] else None,
            "frozen_stages": list(self._state["frozen_stages"]),
            "released_source_files": len(self._state["published_sources"]),
            "working_current_alias": f"working/checkpoints/{self.steps[self._state['current_stage'] - 1]}" if self._state["current_stage"] else None,
            "dynamic_response_support": "host_isolated_bridge" if self.responder is not None else "not_configured_T11_blocked",
        }

    def manifest(self) -> dict:
        """Host metadata only: never return future instructions or file contents."""
        return {
            "mode": "local_research_candidate_only", "official_evaluation": False,
            "stage_count": len(self.steps), "stage_ids": list(self.steps),
            "opening_source_count": len(_files(self.task_root / "environment" / "data" / "source")),
            "stages": [
                {"stage": i, "stage_id": stage_id,
                 "static_release_file_count": len(_files(self.task_root / "steps" / stage_id / "workdir" / "release_materials")),
                 "dynamic_response_required": stage_id == "T11"}
                for i, stage_id in enumerate(self.steps, 1)
            ],
            "final_acceptance": "independent official verifier; not run by this local publisher",
        }
