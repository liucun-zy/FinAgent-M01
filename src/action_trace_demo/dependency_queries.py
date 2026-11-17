"""Shared semantic dependencies over immutable event history.

Operation parents remain audit history. Support, comparison and actual use are
interpreted from their explicit payload fields, including pre-existing records.
"""
from __future__ import annotations


def claim_groups(node):
    p = node['payload']
    return p.get('supports', [[p['source_id']]])


def same_conclusion(target, support):
    """An alternative proves the same fact over the target's entire interval."""
    fields = ('entity', 'field', 'value', 'scope')
    if not all(target.get(k) == support.get(k) for k in fields):
        return False
    lo, hi = target.get('valid_from'), target.get('valid_to')
    source_lo, source_hi = support.get('valid_from'), support.get('valid_to')
    return ((source_lo is None or (lo is not None and source_lo <= lo))
            and (source_hi is None or (hi is not None and hi <= source_hi)))


def group_witness(node, group, index):
    """Return the explicit transformation warrant for one complete alternative."""
    p = node['payload']
    if group == [p.get('source_id')]:
        if len(claim_groups(node)) > 1 and p.get('claim', '').strip() not in p.get('quote', ''):
            return {'verified': False, 'receipt_id': None, 'reason': 'alternative_original_does_not_state_complete_claim'}
        return {'verified': True, 'receipt_id': None, 'reason': 'anchored_original'}
    if len(group) == 1 and index[group[0]]['kind'] == 'claim':
        if same_conclusion(p, index[group[0]]['payload']):
            return {'verified': True, 'receipt_id': None, 'reason': 'same_complete_conclusion'}
    for pid in node.get('parents', []):
        parent = index.get(pid)
        if not parent or parent['kind'] != 'calculation_receipt':
            continue
        receipt = parent['payload']
        if (receipt.get('inputs') == group and receipt.get('entity') == p.get('entity')
                and receipt.get('field') == p.get('field') and receipt.get('result') == p.get('value')
                and receipt.get('purpose') == p.get('scope', {}).get('purpose')):
            return {'verified': True, 'receipt_id': pid, 'reason': 'verified_calculation_receipt'}
    return {'verified': False, 'receipt_id': None, 'reason': 'transformation_not_verified'}


def semantic_edges(parent, child):
    """Classify all roles of an event reference without inferring support."""
    pid, kind, p = parent['id'], child['kind'], child['payload']
    edge = {'from': pid, 'to': child['id']}
    if kind == 'claim':
        result = []
        groups = [i for i, group in enumerate(claim_groups(child)) if pid in group]
        if groups:
            result.append({**edge, 'kind': 'supports', 'support_groups': groups,
                           'relation': 'supported_by' if parent['kind'] == 'source' else 'derived_from'})
        elif pid == p.get('source_id'):
            result.append({**edge, 'kind': 'cites_original', 'relation': 'provenance'})
        if pid in p.get('candidate_ids', []):
            result.append({**edge, 'kind': 'compared_against', 'relation': 'compared_against'})
        return result or [{**edge, 'kind': 'references', 'relation': 'unknown'}]
    role, relation = 'references', 'unknown'
    if kind == 'comparison':
        role = 'old_claim' if pid == p['old']['claim_id'] else 'new_claim' if pid == p['new']['claim_id'] else 'references'
        relation = 'conflicts_with' if role != 'references' else 'unknown'
    elif kind == 'revision':
        role = 'resolves_comparison' if pid == p.get('comparison_id') else 'supports_decision'
        relation = ('supersedes' if role == 'resolves_comparison' and p.get('status') == 'accepted'
                    and p.get('relation') in {'value', 'scope', 'retract'} else 'decision_evidence')
    elif kind in {'probe', 'qa_probe'}:
        role = 'reads_original' if parent['kind'] == 'source' else 'requested_by'
        relation = 'observed_read'
    elif kind == 'artifact_proposal':
        role = 'copies_from' if pid == p.get('source_id') else 'replaces' if pid == p.get('old_id') else 'requires'
        relation = 'used_by' if parent['kind'] == 'claim' else role
    elif kind == 'artifact_version':
        role, relation = 'produces_version', 'produces_version'
    elif kind == 'native_finance_check':
        role = 'observed_input' if parent['kind'] == 'claim' else 'inspects_native_version'
        relation = 'used_by'
    elif kind == 'task_obligation':
        role = 'carries_requirement' if parent['kind'] == 'task_obligation' else 'states_requirement'
        relation = 'requirement'
    elif kind == 'artifact_review_decision':
        role, relation = 'attests_with_' + parent['kind'], 'attests'
    elif kind == 'artifact_mirror_proof':
        role, relation = 'reuses_' + parent['kind'], 'proof_reuse'
    elif kind == 'package_check':
        role = 'compares_version' if parent['kind'] == 'artifact_version' else 'requested_by'
        relation = 'verification'
    elif kind == 'qa_answer':
        role, relation = 'based_on_' + parent['kind'], 'answer_evidence'
    elif kind in {'diagnosis_step', 'artifact_repair_proposal', 'artifact_check', 'qa_route', 'stage_submission'}:
        role, relation = 'uses_' + parent['kind'], 'used_by'
    return [{**edge, 'kind': role, 'relation': relation}]


class DependencyQueries:
    def __init__(self, dag=None, *, nodes=None, claim_view=None):
        self.dag, self.snapshot = dag, nodes
        self.claim_view = claim_view or (dag.claim_at if dag is not None else None)
        self._active_cache = {}

    def _nodes(self, context):
        cutoff = (context or {}).get('as_of')
        nodes = self.snapshot if self.snapshot is not None else self.dag.nodes(as_of=cutoff)
        return [n for n in nodes if cutoff is None or n['seq'] <= cutoff]

    def _active(self, context):
        context = context or {}
        if self.claim_view is None:
            return {n['id'] for n in self._nodes(context) if n['kind'] == 'claim' and not n['payload'].get('pending')}
        key = (context.get('as_of'), context.get('valid_at'))
        if self.snapshot is not None and key in self._active_cache:
            return self._active_cache[key]
        active = {n['id'] for n in self.claim_view(as_of=context.get('as_of'), valid_at=context.get('valid_at'))}
        if self.snapshot is not None:
            self._active_cache[key] = active
        return active

    def dependencies_of(self, artifact_version, context=None):
        context = dict(context or {})
        nodes = self._nodes(context)
        index = {n['id']: n for n in nodes}
        target = artifact_version if isinstance(artifact_version, dict) else index[artifact_version]
        if context.get('as_of') is not None and target['seq'] > context['as_of']:
            raise ValueError('Target is outside the dependency cutoff')
        found, visited = {}, set()

        def add(id_, role, locator=None):
            if id_ not in index:
                raise ValueError('Dependency is outside the known event prefix: ' + id_)
            row = found.setdefault(id_, {'dependency_id': id_, 'roles': [], 'locators': []})
            if role not in row['roles']:
                row['roles'].append(role)
            if locator and locator not in row['locators']:
                row['locators'].append(locator)

        def collect(node):
            if node['id'] in visited:
                return
            visited.add(node['id'])
            p = node['payload']
            if node['kind'] == 'claim':
                for group in claim_groups(node):
                    for dep in group:
                        add(dep, 'support')
                return
            contract = index.get(p.get('contract_id'), {})
            cp = contract.get('payload', {})
            for name, entries in (('contract_rule', cp.get('rules', [])),
                                  ('semantic_rule', cp.get('semantic_rules', [])),
                                  ('binding', p.get('bindings', []))):
                for rule in entries:
                    if rule.get('claim_id'):
                        add(rule['claim_id'], name, rule.get('locator'))
            for name in ('input_claim_ids', 'consumed_claim_ids', 'analysis_input_ids'):
                for dep in p.get(name, []):
                    add(dep, name)
            for dep in contract.get('parents', []):
                if index[dep]['kind'] == 'claim':
                    add(dep, 'contract_input')
            if node['kind'] == 'calculation_receipt':
                for dep in p.get('inputs', []):
                    add(dep, 'calculation_input')
            if node['kind'] == 'comparison':
                for side in ('old', 'new'):
                    add(p[side]['claim_id'], 'comparison_' + side)
            if node['kind'] == 'revision':
                for name in ('old_id', 'new_id'):
                    if p.get(name):
                        add(p[name], 'revision_' + name)
            source = p.get('proof_reused_from') or (p.get('source_id') if p.get('action') in {'copy', 'export'} else None)
            if source in index and index[source]['kind'] == 'artifact_version':
                add(source, 'copied_version')
                collect(index[source])

        collect(target)
        p = target['payload']
        cp = index.get(p.get('contract_id'), {}).get('payload', {})
        context.setdefault('valid_at', cp.get('valid_at'))
        active = self._active(context)
        for row in found.values():
            row['applicable'] = index[row['dependency_id']]['kind'] != 'claim' or row['dependency_id'] in active
        return list(found.values())

    def consumers_of(self, claim_or_relation, context=None, transitive=False):
        context = dict(context or {})
        nodes = self._nodes(context)
        index = {n['id']: n for n in nodes}
        key = claim_or_relation['id'] if isinstance(claim_or_relation, dict) else claim_or_relation
        if key not in index:
            raise ValueError('Changed dependency is outside the known prefix')
        heads = {n['payload']['path']: n['id'] for n in nodes if n['kind'] == 'artifact_version' and n['payload'].get('path')}
        query = DependencyQueries(self.dag, nodes=nodes, claim_view=self.claim_view)
        reached, result = {key}, []
        for node in nodes:
            if node['id'] == key:
                continue
            dependencies = query.dependencies_of(node, context)
            matches = [d for d in dependencies if d['dependency_id'] in reached]
            # Version/action links carry dependency facts forward, without treating
            # comparison candidates as derivations of the new claim.
            if not matches:
                continue
            if transitive and node['kind'] not in {'comparison', 'revision'}:
                reached.add(node['id'])
            p = node['payload']
            if context.get('purpose') is not None and p.get('purpose') != context['purpose']:
                continue
            if context.get('stage') is not None and p.get('stage') != context['stage']:
                continue
            if context.get('latest_heads', True) and node['kind'] == 'artifact_version' and heads.get(p.get('path')) != node['id']:
                continue
            if context.get('kinds') and node['kind'] not in context['kinds']:
                continue
            result.append({'id': node['id'], 'kind': node['kind'], 'node': node,
                           'dependency_ids': [d['dependency_id'] for d in matches],
                           'roles': sorted({r for d in matches for r in d['roles']}),
                           'locators': sorted({r for d in matches for r in d['locators']}),
                           'version_id': node['id'] if node['kind'] == 'artifact_version' else None,
                           'path': p.get('path'), 'purpose': p.get('purpose'), 'sha256': p.get('sha256'),
                           'bindings': [r for r in p.get('bindings', []) if r.get('claim_id') in reached]})
        return result

    def support_bundles(self, target, context=None):
        context = dict(context or {})
        index = {n['id']: n for n in self._nodes(context)}
        target = target['id'] if isinstance(target, dict) else target
        active = self._active(context)

        def build(id_, chain, owner=None):
            node = index[id_]
            p = node['payload']
            if node['kind'] == 'source':
                requirement = owner['payload'] if owner and owner['payload'].get('source_id') == id_ else None
                return {'id': id_, 'kind': 'source', 'available': True, 'source_id': id_,
                        'claim_id': owner['id'] if owner else None,
                        'start': requirement['start'] if requirement else 0,
                        'end': requirement['end'] if requirement else len(p['text']),
                        'quote': requirement['quote'] if requirement else p['text'],
                        'path': [id_, *reversed(chain)]}
            if node['kind'] != 'claim':
                raise ValueError('Support must be an original source or claim')
            groups = []
            for number, group in enumerate(claim_groups(node)):
                if any(dep not in index for dep in group):
                    raise ValueError('Support group is outside the known prefix')
                groups.append({'group_id': f'{id_}:{number}',
                               'required': [build(dep, [*chain, id_], node) for dep in group],
                               'transformation': group_witness(node, group, index)})
            scope_ok = id_ != target or all(p.get('scope', {}).get(k) == v for k, v in context.get('scope', {}).items())
            return {'id': id_, 'kind': 'claim', 'available': id_ in active and scope_ok,
                    'source_id': p['source_id'], 'start': p['start'], 'end': p['end'], 'quote': p['quote'],
                    'groups': groups, 'transformation': {'verified': False, 'reason': 'use_group_witness'}}

        return build(target, [])

    def impact(self, change, context=None):
        context = dict(context or {})
        ids = [change] if isinstance(change, str) else list(change)
        nodes = self._nodes(context)
        index = {n['id']: n for n in nodes}
        if not set(ids) <= index.keys():
            raise ValueError('Changed dependency is outside the known prefix')
        active = self._active(context)

        def available(id_):
            return index[id_]['kind'] != 'claim' or id_ in active

        potential, invalidated = set(ids), set(ids)
        for node in nodes:
            if node['kind'] != 'claim' or node['id'] in ids:
                continue
            groups = claim_groups(node)
            if any(dep in potential for group in groups for dep in group):
                potential.add(node['id'])
            touched = any(dep in invalidated for group in groups for dep in group)
            surviving = [group for group in groups
                         if all(dep not in invalidated and available(dep) for dep in group)]
            if len(groups) > 1:
                surviving = [group for group in surviving if group_witness(node, group, index)['verified']]
            if touched and not surviving:
                invalidated.add(node['id'])
        consumers = {}
        if context.get('include_consumers', True):
            for id_ in potential:
                for row in self.consumers_of(id_, {**context, 'kinds': ['artifact_version']}):
                    consumers[row['id']] = row
        return {'potential': [n['id'] for n in nodes if n['id'] in potential],
                'invalidated': [n['id'] for n in nodes if n['id'] in invalidated],
                'consumers': sorted(consumers.values(), key=lambda row: row['node']['seq'])}
