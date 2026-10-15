"""Bounded offline composition planning. No execution, discovery or promotion authority."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import stat

MAX_BYTES = 1024 * 1024
NAME = re.compile(r"[a-z][a-z0-9._-]{0,79}\Z")
EDITION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,79}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")


class CompositionError(ValueError):
    pass


def _bounded(value):
    pending = [(value, 0)]; seen = 0; containers = set()
    while pending:
        item, depth = pending.pop(); seen += 1
        if depth > 32 or seen > 30000:
            raise CompositionError('document_limit')
        if type(item) in (dict, list):
            # JSON is a tree. Reject aliased/cyclic host objects too.
            if id(item) in containers:
                raise CompositionError('aliased_or_cyclic_document')
            containers.add(id(item))
            if len(item) > 2048:
                raise CompositionError('document_limit')
            if type(item) is dict:
                if any(type(k) is not str or len(k) > 160 for k in item):
                    raise CompositionError('invalid_key')
                pending.extend((v, depth + 1) for v in item.values())
            else:
                pending.extend((v, depth + 1) for v in item)
        elif type(item) is str:
            if len(item) > 1024:
                raise CompositionError('string_limit')
        elif item is not None and type(item) not in (bool, int):
            raise CompositionError('unsupported_scalar')
        elif type(item) is int and item.bit_length() > 64:
            raise CompositionError('integer_limit')


def digest(value):
    _bounded(value)
    try:
        data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()
    except (ValueError, TypeError, RecursionError) as error:
        raise CompositionError('invalid_document') from error
    if len(data) > MAX_BYTES:
        raise CompositionError('document_limit')
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise CompositionError('duplicate_key')
            result[key] = value
        return result
    try:
        fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
                raise CompositionError('unsafe_or_oversize_file')
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise CompositionError('document_limit')
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(CompositionError('nonfinite_number')))
        digest(value)
        return value
    except CompositionError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise CompositionError('unreadable_or_invalid_json') from error


def _keys(value, keys):
    if type(value) is not dict or set(value) != set(keys.split()):
        raise CompositionError('invalid_fields')


def _text(value, pattern=NAME):
    if type(value) is not str or not pattern.fullmatch(value):
        raise CompositionError('invalid_identifier_or_hash')


def _roots(roots, nodes):
    if type(roots) is not list or not 1 <= len(roots) <= 8:
        raise CompositionError('invalid_roots')
    for root in roots:
        _text(root)
    if len(set(roots)) != len(roots) or any(root not in nodes for root in roots):
        raise CompositionError('missing_or_duplicate_root')


def _pin_errors(graph):
    nodes = {n['id']: n for n in graph['nodes']}; errors = []
    for node in graph['nodes']:
        for required in node['requires']:
            target = nodes[required['id']]
            if any(required[k] != target[k] for k in ('edition', 'artifactSha256', 'contractSha256')):
                errors.append({'kind': 'stale_dependency_pin', 'consumer': node['id'], 'dependency': target['id']})
    for edge in graph['connections']:
        source, target = nodes[edge['fromNode']], nodes[edge['toNode']]
        if source['outputs'][edge['fromPort']] != target['inputs'][edge['toPort']]:
            errors.append({'kind': 'port_schema_mismatch', 'consumer': target['id'], 'dependency': source['id']})
    return sorted(errors, key=lambda x: (x['consumer'], x['dependency'], x['kind']))


def validate_graph(graph, expected_scope, verify_pins=True):
    digest(graph)
    _text(expected_scope)
    _keys(graph, 'schema scope nodes connections')
    if graph['scope'] != expected_scope:
        raise CompositionError('scope_denied')
    if graph['schema'] != 'gradia.composition.graph.v1':
        raise CompositionError('unsupported_schema')
    if type(graph['nodes']) is not list or not 1 <= len(graph['nodes']) <= 128:
        raise CompositionError('node_limit')
    if type(graph['connections']) is not list or len(graph['connections']) > 512:
        raise CompositionError('connection_limit')
    nodes = {}; dependencies = 0
    for node in graph['nodes']:
        _keys(node, 'id kind edition artifactSha256 contractSha256 scope requires inputs outputs')
        # Scope refusal intentionally does not include foreign IDs or names.
        if node['scope'] not in ('public', expected_scope):
            raise CompositionError('scope_denied')
        _text(node['id']); _text(node['edition'], EDITION)
        _text(node['artifactSha256'], SHA); _text(node['contractSha256'], SHA)
        if node['kind'] not in ('bridge', 'block', 'workflow', 'variant', 'skill'):
            raise CompositionError('invalid_kind')
        if node['id'] in nodes:
            raise CompositionError('duplicate_node')
        nodes[node['id']] = node
        if type(node['requires']) is not list or len(node['requires']) > 128:
            raise CompositionError('dependency_limit')
        dependencies += len(node['requires'])
        for direction in ('inputs', 'outputs'):
            ports = node[direction]
            if type(ports) is not dict or len(ports) > 32:
                raise CompositionError('port_limit')
            for name, schema in ports.items():
                _text(name); _text(schema, SHA)
    if dependencies > 512:
        raise CompositionError('dependency_limit')
    for node in nodes.values():
        seen = set()
        for pin in node['requires']:
            _keys(pin, 'id edition artifactSha256 contractSha256')
            _text(pin['id']); _text(pin['edition'], EDITION)
            _text(pin['artifactSha256'], SHA); _text(pin['contractSha256'], SHA)
            if pin['id'] not in nodes or pin['id'] in seen:
                raise CompositionError('missing_or_duplicate_dependency')
            if node['scope'] == 'public' and nodes[pin['id']]['scope'] != 'public':
                raise CompositionError('scope_denied')
            seen.add(pin['id'])
    active, done = set(), set()
    def visit(key):
        if key in active:
            raise CompositionError('dependency_cycle')
        if key in done:
            return
        active.add(key)
        for pin in nodes[key]['requires']:
            visit(pin['id'])
        active.remove(key); done.add(key)
    for key in nodes:
        visit(key)
    wired = set(); edges = set()
    for edge in graph['connections']:
        _keys(edge, 'fromNode fromPort toNode toPort')
        for value in edge.values():
            _text(value)
        source, target = nodes.get(edge['fromNode']), nodes.get(edge['toNode'])
        if source is None or target is None or edge['fromPort'] not in source['outputs'] or edge['toPort'] not in target['inputs']:
            raise CompositionError('missing_connection_endpoint')
        if source['id'] not in {pin['id'] for pin in target['requires']}:
            raise CompositionError('undeclared_connection_dependency')
        identity = tuple(edge[k] for k in ('fromNode','fromPort','toNode','toPort'))
        inlet = (target['id'], edge['toPort'])
        if identity in edges or inlet in wired:
            raise CompositionError('duplicate_connection_or_input')
        edges.add(identity); wired.add(inlet)
    if verify_pins and _pin_errors(graph):
        raise CompositionError('dependency_pin_or_port_mismatch')
    # Defensive snapshot: caller mutation after validation cannot change our returned graph.
    return json.loads(json.dumps(graph))


def build_lock(graph, roots, expected_scope):
    graph = validate_graph(graph, expected_scope)
    nodes = {n['id']: n for n in graph['nodes']}; _roots(roots, nodes)
    selected = set()
    def include(key):
        if key not in selected:
            selected.add(key)
            for pin in nodes[key]['requires']:
                include(pin['id'])
    for key in roots:
        include(key)
    connections = sorted((e for e in graph['connections'] if e['fromNode'] in selected and e['toNode'] in selected), key=lambda e: tuple(e[k] for k in sorted(e)))
    wired = {(e['toNode'], e['toPort']) for e in connections}
    unresolved = [{'node': key, 'port': port} for key in sorted(selected) for port in sorted(nodes[key]['inputs']) if (key,port) not in wired]
    lock = {'schema':'gradia.composition.lock.v1', 'scope':expected_scope, 'graphSha256':digest(graph), 'roots':sorted(roots),
            'nodes': [nodes[key] for key in sorted(selected)], 'connections':connections, 'unresolvedInputs':unresolved,
            'compatibility':'UNKNOWN', 'promotionAuthorized':False, 'artifactVerification':'DECLARED_HASHES_ONLY',
            'boundary':'Offline structural plan; no execution, behavioral evaluation, runtime attestation or promotion'}
    return {**lock, 'lockSha256':digest(lock)}


def compare_graphs(baseline, candidate, roots, expected_scope):
    baseline = validate_graph(baseline, expected_scope)
    candidate = validate_graph(candidate, expected_scope, verify_pins=False)
    before = {n['id']:n for n in baseline['nodes']}; after = {n['id']:n for n in candidate['nodes']}
    _roots(roots, before)
    changed = {key for key in set(before)|set(after) if before.get(key) != after.get(key)}
    # Wiring itself changes a consumer's semantics even when node metadata stays identical.
    edge_key = lambda e: tuple(e[k] for k in ('fromNode','fromPort','toNode','toPort'))
    old_edges = {edge_key(e) for e in baseline['connections']}; new_edges = {edge_key(e) for e in candidate['connections']}
    changed.update(edge[2] for edge in old_edges ^ new_edges)
    reverse = {}
    for graph in (baseline, candidate):
        for node in graph['nodes']:
            for pin in node['requires']:
                reverse.setdefault(pin['id'], set()).add(node['id'])
    affected = set(changed); pending = list(changed)
    while pending:
        for consumer in reverse.get(pending.pop(), ()):
            if consumer not in affected:
                affected.add(consumer); pending.append(consumer)
    blockers = _pin_errors(candidate)
    blockers.extend({'kind':'removed_root','consumer':key} for key in sorted(set(roots)-set(after)))
    report = {'schema':'gradia.composition.impact.v1','scope':expected_scope,'baselineGraphSha256':digest(baseline),
              'candidateGraphSha256':digest(candidate),'baselineLockSha256':build_lock(baseline,roots,expected_scope)['lockSha256'],
              'changed':sorted(changed),'affected':sorted(affected),'affectedRoots':sorted(set(roots)&affected),
              'requiredVerification': [{'node':key,'kind':(after.get(key) or before[key])['kind'],'status':'UNKNOWN'} for key in sorted(affected)],
              'blockers':blockers,'candidateLock':None if blockers else build_lock(candidate,roots,expected_scope),
              'graphCoverage':'SUPPLIED_SNAPSHOT_ONLY','compatibility':'UNKNOWN','promotionAuthorized':False}
    return {**report,'reportSha256':digest(report)}


def verify_lock(lock, graph, roots, expected_scope):
    """Reproduce from trusted scope/roots and supplied snapshot; a digest is not a signature."""
    digest(lock)
    expected = build_lock(graph, roots, expected_scope)
    # Python equality conflates False/0 and True/1; canonical JSON identities must not.
    if digest(lock) != digest(expected):
        raise CompositionError('lock_identity_mismatch')
    return {'schema':'gradia.composition.lock-verification.v1', 'lockSha256':expected['lockSha256'],
            'status':'REPRODUCED', 'compatibility':'UNKNOWN', 'promotionAuthorized':False}


def diagram(graph, expected_scope):
    graph = validate_graph(graph, expected_scope, verify_pins=False)
    # Labels pass restrictive identifier validation; generated node IDs are never user syntax.
    ids = {node['id']:f'n{index}' for index,node in enumerate(sorted(graph['nodes'], key=lambda n:n['id']))}
    lines = ['flowchart LR']
    for node in sorted(graph['nodes'], key=lambda n:n['id']):
        lines.append(f'  {ids[node["id"]]}["{node["id"]} | {node["kind"]} | {node["edition"]}"]')
    for node in sorted(graph['nodes'], key=lambda n:n['id']):
        for pin in sorted(node['requires'], key=lambda p:p['id']):
            lines.append(f'  {ids[pin["id"]]} --> {ids[node["id"]]}')
    return '\n'.join(lines) + '\n'
