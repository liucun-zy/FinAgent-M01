"""Resume only the audited T11 Responder release failure after ten frozen stages.

The host must first prepare a valid, request/config-bound Responder cache. This
module never parses/repairs NPC JSON or writes a receipt/public response: the
unchanged real bridge and official commit path own release and validation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import uuid

from .benchmark import DeliverBenchCase
from .model import ModelClient
from .responder import ResponderBridge
from .runner import SYSTEM, _expected_present, append, export_run, memory_packet, run_loop, save
from .tools import WorkspaceTools
from .trace import TraceStore


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resume_release_failure(run_dir: Path, config: dict, *, through_stage: int = 13) -> dict:
    run_dir = Path(run_dir).resolve()
    if not isinstance(through_stage, int) or isinstance(through_stage, bool) or not 11 <= through_stage <= 13:
        raise ValueError('Recovery may continue only stages 11 through 13')
    status = json.loads((run_dir / 'run_status.json').read_text())
    if (status.get('state') != 'failed' or status.get('error_type') != 'DynamicResponseRequired'
            or status.get('error') != 'T11 real Responder failed; no T11 materials were released'
            or status.get('current_stage') != 10 or status.get('completed_stages') != list(range(1, 11))):
        raise ValueError('Only the pre-release T11 DynamicResponseRequired failure after ten frozen stages is eligible')
    manifest = json.loads((run_dir / 'run_manifest.json').read_text())
    expected = {'candidate_max_turns_per_stage': 35, 'candidate_force_final_turn': True,
                'context_policy': 'stage_ledger'}
    for key, value in expected.items():
        if manifest.get(key) != value or config.get(key, False if key == 'candidate_force_final_turn' else None) != value:
            raise ValueError(f'Candidate recovery policy differs from original: {key}')
    if config.get('context_limit_chars', 0) != manifest.get('context_limit_chars', 0):
        raise ValueError('Candidate context limit changed')
    settings = dict(config)
    if isinstance(config.get('model'), dict):
        settings.update(config['model'])
    model = settings.get('model')
    model = model if isinstance(model, str) else settings.get('model_name', 'gpt-6-luna')
    if model != manifest.get('model'):
        raise ValueError('Candidate model changed')
    if not config.get('enable_responder', True):
        raise ValueError('Real Responder bridge is required')
    case_root = Path(manifest['case_root']).resolve()
    state_path = run_dir / '.workspace.case_state.json'
    state = json.loads(state_path.read_text())
    frozen_ids = [f'T{stage:02d}' for stage in range(1, 11)]
    if (state.get('current_stage') != 10 or state.get('frozen_stages') != frozen_ids
            or Path(state.get('case_root', '')).resolve() != case_root):
        raise ValueError('Workspace is not the original frozen T10 checkpoint')
    if (run_dir / 'workspace/working/checkpoints/T11').exists():
        raise ValueError('T11 checkpoint already exists; do not replay Candidate work')
    visibility = json.loads((run_dir / 'source_visibility.json').read_text())
    if any(not isinstance(stage, int) or stage > 10 for stage in visibility.values()):
        raise ValueError('Future source visibility already exists')
    audit_log = run_dir / 'model_calls.jsonl'
    for line in audit_log.read_text().splitlines() if audit_log.exists() else []:
        row = json.loads(line)
        if row.get('task') in {'T11', 'T12', 'T13'}:
            raise ValueError('Later Candidate model calls already exist; replay forbidden')
    bridge = ResponderBridge(case_root, run_dir, config)
    binding = json.loads((run_dir / 'private_responder/P02_LENDER.binding.json').read_text())
    if binding.get('configuration_sha256') != bridge.configuration_sha256:
        raise ValueError('Responder configuration/code hash changed')
    if any((run_dir / 'private_responder/sessions').glob('*/publication.json')):
        raise ValueError('A dynamic response has already been published')
    case = DeliverBenchCase(case_root, run_dir / 'workspace', responder=bridge)
    observed = case.status()  # Verifies every published source and frozen file.
    if observed.get('current_stage') != 10 or observed.get('frozen_stages') != frozen_ids:
        raise ValueError('Verified workspace stage differs from failed release boundary')
    frozen_hashes = {unit: _hash(run_dir / 'freezes/workspace' / unit / 'freeze.json') for unit in frozen_ids}
    if any(state.get('freeze_record_hashes', {}).get(unit) != digest for unit, digest in frozen_hashes.items()):
        raise ValueError('Original freeze record hash changed')
    audit = run_dir / 'recoveries' / ('release11_' + uuid.uuid4().hex[:10])
    audit.mkdir(parents=True)
    for filename in ('run_status.json', 'conversation.json', '.workspace.case_state.json'):
        if (run_dir / filename).exists():
            shutil.copy2(run_dir / filename, audit / ('original_' + filename.lstrip('.')))
    recovery = {'kind': 'technical_pre_release_T11_recovery', 'resume_from_stage': 11,
                'current_stage': 10, 'candidate_outputs_regenerated': False,
                'historical_candidate_calls_replayed': False, 'original_freeze_record_sha256': frozen_hashes,
                'responder_configuration_sha256': bridge.configuration_sha256,
                'candidate_policy_verified': {**expected, 'context_limit_chars': manifest.get('context_limit_chars', 0), 'model': model},
                'original_full_configuration_hash_available': False,
                'policy_change': 'No Candidate policy change; retry unchanged real bridge/official commit with host-audited cache',
                'module_sha256': _hash(Path(__file__))}
    save(audit / 'recovery_manifest.json', recovery)
    status.update(state='running', recovery_count=status.get('recovery_count', 0) + 1,
                  recovery_audit=str(audit.relative_to(run_dir)))
    status.pop('error', None); status.pop('error_type', None)
    save(run_dir / 'run_status.json', status)
    trace = TraceStore(run_dir)
    log = run_dir / 'agent_trajectory.jsonl'
    append(log, {'task': 'technical_recovery', 'stage': 10, 'resume_from_stage': 11,
                 'recovery_audit': str(audit.relative_to(run_dir))})
    client = None
    try:
        for stage in range(11, through_stage + 1):
            # Only this unchanged path can commit the real NPC response and
            # publish dynamic/static material. No T11 prompt precedes success.
            info = case.release(stage)
            visibility.update({path: stage for path in info.get('released_files', [])})
            save(run_dir / 'source_visibility.json', visibility)
            status.update(state='running', current_stage=stage)
            save(run_dir / 'run_status.json', status)
            if client is None:
                client = ModelClient(config, run_dir)
            tools = WorkspaceTools(run_dir / 'workspace', run_dir, trace, stage)
            recent = []
            for previous in status['completed_stages'][-3:]:
                row = json.loads((run_dir / 'stage_results' / f'T{previous:02d}.json').read_text())
                recent.append({'stage': previous, 'completion_note': str(row.get('response', ''))[:4000]})
            messages = [{'role': 'system', 'content': SYSTEM},
                        {'role': 'user', 'content': '<project_memory>' + memory_packet(tools) + '\nRecent completion notes: ' + json.dumps(recent, ensure_ascii=False) + '</project_memory>'},
                        {'role': 'user', 'content': f'<stage_request id="T{stage:02d}">\n' + info['instruction'] + '\nNewly released files: ' + json.dumps(info.get('released_files', []), ensure_ascii=False) + '\n</stage_request>'}]
            save(run_dir / 'conversation.json', messages)
            remaining = 35
            for attempt in range(3):
                result = run_loop(client, tools, messages, max_turns=remaining, task=f'T{stage:02d}', log=log,
                                  context_limit_chars=int(config.get('context_limit_chars', 0)), turn_offset=35 - remaining,
                                  force_final_turn=True)
                messages = result['messages']; remaining -= result['turns']
                found, missing = _expected_present(info, tools)
                if result['status'] != 'responded':
                    status.update(state='candidate_' + result['status'], missing_outputs=missing); break
                if not missing:
                    frozen = case.freeze(stage)
                    if frozen.get('submission_complete') is False:
                        status.update(state='incomplete_submission', freeze=frozen); break
                    status['completed_stages'].append(stage)
                    save(run_dir / 'stage_results' / f'T{stage:02d}.json', {'response': result['answer'], 'found_outputs': found, 'freeze': frozen})
                    break
                messages.append({'role': 'user', 'content': 'The requested deliverables are still missing or empty: ' + json.dumps(missing) + '. Complete them before ending this stage.'})
            else:
                status.update(state='incomplete_submission', missing_outputs=missing)
            save(run_dir / 'conversation.json', messages)
            if stage not in status['completed_stages']:
                break
        else:
            status.update(state='requested_stages_completed')
    except Exception as exc:
        status.update(state='failed', error_type=type(exc).__name__, error=str(exc)[:1000])
    # A failed release makes no Candidate call and leaves its old conversation.
    # Successful continuation must preserve all ten historical freezes exactly.
    preserved = all(_hash(run_dir / 'freezes/workspace' / unit / 'freeze.json') == digest for unit, digest in frozen_hashes.items())
    recovery['original_freeze_hashes_preserved'] = preserved
    save(audit / 'recovery_manifest.json', recovery)
    if not preserved:
        status.update(state='failed', error_type='FrozenEvidenceChanged', error='Historical freeze records changed during continuation')
    save(run_dir / 'run_status.json', status)
    if client is None:
        return {'run_dir': str(run_dir), 'status': status, 'recovery_audit': str(audit)}
    return export_run(run_dir, trace, client, status)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--through-stage', type=int, default=13)
    args = parser.parse_args()
    print(json.dumps(resume_release_failure(args.run_dir, json.loads(args.config.read_text()), through_stage=args.through_stage), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
