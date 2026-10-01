"""Compile capture/completion histories into checked native schedules.

Each certificate records a trace-specific check in the declared finite model.
Reordering preserves replacement authority and protected effects; final replay
checks the native entry conditions.
"""
from dataclasses import asdict

from contract_model import initial, step, alphabet


class LoweringRejected(ValueError):
    pass


def _events(events):
    if not events or any(not e.startswith(('request_', 'complete_')) for e in events):
        raise LoweringRejected('only registered capture/completion events are supported')
    captures = [e for e in events if e.startswith('request_')]
    if len(captures) != len(set(captures)):
        raise LoweringRejected('each live channel must identify exactly one native request')
    completions = [e.split('_')[1] for e in events if e.startswith('complete_')]
    if len(completions) != len(set(completions)):
        raise LoweringRejected('a native HTTP request completes once')
    if set(completions) != {e.split('_')[1] for e in captures}:
        raise LoweringRejected('each native request needs a capture and completion')


def semantic_trace(events, decl, native_capture_states=None):
    _events(events)
    if any(event not in alphabet(decl) for event in events):
        raise LoweringRejected('event outside the declared operation alphabet')
    state = initial(decl)
    names, captures, effects = {}, {}, []

    def context(value, object_id):
        return tuple((d, object_id if d == 'object' else getattr(value, d))
                     for d in decl.dimensions)

    def projection(s):
        return (s.selected, tuple((context(o, i), o.state,
                    None if o.approval is None else
                    (names[o.approval.receipt.identifier],
                     context(o.approval.receipt, o.approval.receipt.object),
                     o.approval.positive)) for i, o in enumerate(s.objects)))

    for event in events:
        if event.startswith('request_') and native_capture_states is not None:
            if state.current.state not in native_capture_states:
                raise LoweringRejected(f'native capture disabled at {event}: {state.current.state}')
        transition = step(state, event, decl)
        if transition is None:
            raise LoweringRejected(f'abstract event disabled: {event}')
        following, concrete, facts = transition
        if event.startswith('request_'):
            receipt = following.slot(event.split('_')[1])
            names[receipt.identifier] = event
            captures[event] = context(receipt, receipt.object)
        else:
            # Includes effect-time authority, sign, input validity and the
            # protected object's before/after projection, independently of slots.
            observed = {k: v for k, v in facts.items() if k != 'incoming'}
            observed['incoming_capture'] = names[facts['incoming']]
            effects.append((event, observed, projection(state), projection(following)))
        state = following
    return dict(captures=sorted(captures.items()), effects=effects, final=projection(state))


def footprints(event, decl):
    if event.startswith('request_'):
        writes = ['request_slot', 'fresh_identity']
        if decl.identity == 'latest':
            writes += ['latest', 'approval']
        return {'reads': list(decl.dimensions), 'writes': writes}
    return {'reads': list(decl.dimensions) + ['state', 'approval', 'request_origin'],
            'writes': ['approval', 'state'] + (['request_slot'] if decl.consume_receipt_on_completion else [])}


def certify_reordering(original, reordered, decl, capture_states=('pending',)):
    """Replay every adjacent capture hoist; reject changed authority/effects."""
    original, reordered = list(original), list(reordered)
    _events(original); _events(reordered)
    if sorted(original) != sorted(reordered):
        raise LoweringRejected('schedule changed the event multiset')
    for prefix in ('request_', 'complete_'):
        if [e for e in original if e.startswith(prefix)] != [e for e in reordered if e.startswith(prefix)]:
            raise LoweringRejected('capture order and completion order must each be preserved')
    reference = semantic_trace(original, decl)
    current, swaps = list(original), []
    for dest, event in enumerate(reordered):
        source = current.index(event)
        while source > dest:
            left, right = current[source-1:source+1]
            if not (right.startswith('request_') and left.startswith('complete_')):
                raise LoweringRejected('transformation is outside capture-hoisting rule')
            candidate = current.copy()
            candidate[source-1:source+1] = [right, left]
            if semantic_trace(candidate, decl) != reference:
                raise LoweringRejected(f'authority or protected effect changes: {left} / {right}')
            swaps.append({'left': left, 'right': right, 'prefix': current[:source-1],
                          'left_footprint': footprints(left, decl),
                          'right_footprint': footprints(right, decl),
                          'capture_contexts_equal': True, 'authority_facts_equal': True,
                          'protected_effects_equal': True, 'abstract_events_enabled': True})
            current = candidate; source -= 1
    native = semantic_trace(reordered, decl, tuple(capture_states))
    if native != reference:
        raise LoweringRejected('final native history changes the relation')
    return dict(original=original, schedule=reordered, swaps=swaps,
                native_capture_states=list(capture_states), native_route_enabled=True,
                semantic_projection=reference, declaration=asdict(decl),
                guarantee='trace-specific declared-model equivalence; registered native hooks required')


def compile_schedule(events, decl, capture_states=('pending',)):
    """Compile a dependency DAG and select a checked legal schedule."""
    events = list(events); _events(events)
    captures = [e for e in events if e.startswith('request_')]
    completions = [e for e in events if e.startswith('complete_')]
    edges = set(zip(captures, captures[1:])) | set(zip(completions, completions[1:]))
    edges |= {(f'request_{e.split("_")[1]}', e) for e in completions}
    # Semantic hazards constrain the final schedule through certificate checking.
    # Preserve completion order: it embodies the abstract relation target.
    candidates, rejected = [], []

    def visit(prefix):
        if len(prefix) == len(events):
            candidates.append(prefix); return
        for event in events:
            if event not in prefix and all(a in prefix for a, b in edges if b == event):
                visit(prefix + [event])
    visit([])
    for candidate in candidates:
        try:
            certificate = certify_reordering(events, candidate, decl, capture_states)
        except LoweringRejected as error:
            rejected.append({'schedule': candidate, 'reason': str(error)})
            continue
        certificate['dependencies'] = [list(e) for e in sorted(edges)]
        certificate['rejected_candidates'] = rejected
        return certificate
    raise LoweringRejected('no equivalent native schedule: ' + repr(rejected))
