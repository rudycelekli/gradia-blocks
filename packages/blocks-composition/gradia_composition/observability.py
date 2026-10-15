"""Optional metadata receipt envelope; no collector, raw-input logging or execution."""
import hashlib
import time
import uuid
from datetime import datetime, timezone
from .core import CompositionError, build_lock, compare_graphs, diagram, digest, verify_lock


def observe_plan(operation, *, graph, expected_scope, roots=None, candidate=None, lock=None):
    """Host may persist receipt separately. Full result remains within authorized caller scope."""
    started = time.monotonic()
    receipt = {'schema':'gradia.composition.observation.v1', 'correlationId':str(uuid.uuid4()),
               'occurredAt':datetime.now(timezone.utc).isoformat(), 'operation':'unknown',
               'status':'REFUSED', 'reason':None, 'artifactSha256':None,
               'cost':{'status':'unknown','amount':None}, 'coverage':'this_call_only',
               'promotionAuthorized':False}
    result = None
    try:
        if type(operation) is not str or operation not in ('lock','impact','diagram','verify-lock'):
            raise CompositionError('unsupported_operation')
        receipt['operation'] = operation
        if operation=='lock':
            result=build_lock(graph,roots,expected_scope)
        elif operation=='impact':
            result=compare_graphs(graph,candidate,roots,expected_scope)
        elif operation=='verify-lock':
            result=verify_lock(lock,graph,roots,expected_scope)
        else:
            result=diagram(graph,expected_scope)
        receipt['artifactSha256'] = digest(result) if type(result) is dict else hashlib.sha256(result.encode()).hexdigest()
        receipt['status']='PRODUCED'
    except CompositionError as error:
        # Error codes are fixed by this module; never reflect incoming values or identifiers.
        result = None
        receipt['reason']=str(error)
    receipt['durationMs']=round((time.monotonic()-started)*1000,3)
    return {'result':result,'receipt':receipt}
