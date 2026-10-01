"""Finite approval model shared by relation and coverage constructors.

The abstraction preserves receipt identities, aliases, and equality relations
between bound versions. Counter values are consistently renamed. Searches record
their depth and transition limits; unresolved targets remain bounded-unreached.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, replace
import json
import random


@dataclass(frozen=True)
class Declaration:
    dimensions: tuple[str, ...] = ('object', 'content', 'authority')
    completion_states: tuple[str, ...] = ('pending',)
    activation_states: tuple[str, ...] = ('pending', 'suspended')
    batch_states: tuple[str, ...] = ('pending',)
    states: tuple[str, ...] = ('pending', 'active', 'suspended')
    consumed_state: str = 'active'
    reset_state: str = 'pending'
    perturb: tuple[tuple[str, str], ...] = (('content', 'edit'), ('authority', 'revoke'))
    lifecycle: tuple[tuple[str, str], ...] = (('suspend', 'suspended'),)
    channels: tuple[str, ...] = ('a', 'b')
    object_count: int = 1
    malformed_inputs: bool = False
    identity: str = 'value'  # value, revision, latest; explicit declared policy
    positive_completion_state: str | None = None
    negative_completion_state: str | None = None
    consume_receipt_on_completion: bool = False

    def validate(self):
        known = {'object', 'content', 'authority', 'revision', 'latest', 'purpose'}
        assert set(self.dimensions) <= known
        assert self.identity in {'value', 'revision', 'latest'}
        assert self.object_count in (1, 2)
        assert self.consumed_state in self.states
        assert all(set(x) <= set(self.states) for x in
                   (self.completion_states, self.activation_states, self.batch_states))
        assert all(d in known for d, _ in self.perturb)


@dataclass(frozen=True)
class Receipt:
    identifier: int
    object: int
    content: str
    authority: int
    revision: int
    latest: int
    purpose: str


@dataclass(frozen=True)
class Approval:
    receipt: Receipt
    positive: bool


@dataclass(frozen=True)
class Object:
    content: str = 'a'
    authority: int = 0
    revision: int = 0
    latest: int = 0
    purpose: str = 'review'
    state: str = 'pending'
    approval: Approval | None = None


@dataclass(frozen=True)
class State:
    objects: tuple[Object, ...] = (Object(),)
    selected: int = 0
    slots: tuple[tuple[str, Receipt], ...] = ()
    next_identifier: int = 0

    @property
    def current(self):
        return self.objects[self.selected]

    def slot(self, channel):
        return dict(self.slots).get(channel)


def initial(decl):
    decl.validate()
    return State(objects=tuple(Object(state=decl.reset_state) for _ in range(decl.object_count)))


def dimension(obj, object_id, name):
    return object_id if name == 'object' else getattr(obj, name)


def matches(receipt, obj, object_id, decl):
    return receipt is not None and all(getattr(receipt, d) == dimension(obj, object_id, d)
                                      for d in decl.dimensions)


def valid(state, decl):
    approval = state.current.approval
    return bool(approval and approval.positive and
                matches(approval.receipt, state.current, state.selected, decl))


def alphabet(decl):
    ops = []
    for channel in decl.channels:
        ops += [f'request_{channel}', f'complete_{channel}_pos', f'complete_{channel}_neg']
        if decl.malformed_inputs:
            ops += [f'complete_{channel}_badpos', f'complete_{channel}_badneg']
    ops += [name for _, name in decl.perturb]
    ops += [name for name, _ in decl.lifecycle]
    if decl.object_count > 1:
        ops += [f'select_{i}' for i in range(decl.object_count)]
    if decl.activation_states:
        ops.append('activate')
    if decl.batch_states:
        ops.append('activate_batch')
    return tuple(ops)


def put(state, obj):
    objects = list(state.objects); objects[state.selected] = obj
    return replace(state, objects=tuple(objects))


def failing(state, receipt, input_valid, decl):
    reasons = []
    if state.current.state not in decl.completion_states:
        reasons.append('state:' + state.current.state)
    if not input_valid:
        reasons.append('input')
    for name in decl.dimensions:
        if getattr(receipt, name) != dimension(state.current, state.selected, name):
            reasons.append('dim:' + name)
    return tuple(reasons)


def step(state, operation, decl):
    """Declared effect; (next, concrete event, transition facts), or disabled None."""
    obj = state.current
    info = {'operation': operation, 'state_before': obj.state,
            'valid_before': valid(state, decl), 'object': state.selected}
    if operation.startswith('select_'):
        selected = int(operation.split('_')[1])
        if selected == state.selected:
            return None
        return replace(state, selected=selected), operation, info
    if operation.startswith('request_'):
        channel = operation.split('_')[1]
        if decl.identity == 'latest' and obj.state not in decl.completion_states:
            return None
        if decl.identity == 'latest':
            obj = replace(obj, latest=obj.latest + 1, approval=None)
            state = put(state, obj)
        receipt = Receipt(state.next_identifier, state.selected, obj.content,
                          obj.authority, obj.revision, obj.latest, obj.purpose)
        slots = dict(state.slots); slots[channel] = receipt
        return replace(state, slots=tuple(sorted(slots.items())),
                       next_identifier=state.next_identifier + 1), operation, info
    if operation.startswith('complete_'):
        _, channel, sign = operation.split('_')
        receipt = state.slot(channel)
        if receipt is None:
            return None
        input_valid = not sign.startswith('bad')
        positive = sign.endswith('pos')
        reasons = failing(state, receipt, input_valid, decl)
        info.update(complete=True, sign='pos' if positive else 'neg',
                    incoming_valid=input_valid, why=reasons, incoming=receipt.identifier)
        if not reasons:
            destination = (decl.positive_completion_state if positive else
                           decl.negative_completion_state)
            state = put(state, replace(obj, approval=Approval(receipt, positive),
                                       state=destination or obj.state))
        if decl.consume_receipt_on_completion:
            slots = dict(state.slots); slots.pop(channel)
            state = replace(state, slots=tuple(sorted(slots.items())))
        return state, operation, info
    for name, event in decl.perturb:
        if operation == event:
            if name == 'content':
                obj = replace(obj, content='b' if obj.content == 'a' else 'a',
                              revision=obj.revision + 1)
                concrete = 'edit_' + obj.content
            elif name == 'purpose':
                obj = replace(obj, purpose='publish' if obj.purpose == 'review' else 'review')
                concrete = event
            else:
                obj = replace(obj, **{name: getattr(obj, name) + 1})
                concrete = event
            return put(state, replace(obj, state=decl.reset_state, approval=None)), concrete, info
    for operation_name, destination in decl.lifecycle:
        if operation == operation_name:
            if obj.state == destination:
                return None
            return put(state, replace(obj, state=destination)), operation, info
    if operation in ('activate', 'activate_batch'):
        entry = 'batch' if operation == 'activate_batch' else 'single'
        allowed = decl.batch_states if entry == 'batch' else decl.activation_states
        qualified = valid(state, decl) and obj.state in allowed
        info.update(activate=entry, eligible=qualified)
        if qualified:
            state = put(state, replace(obj, state=decl.consumed_state))
        return state, operation, info
    raise ValueError(operation)


def key(state, decl):
    """Preserve binding equality, origin identity, and slot aliases in state keys.

    Counter renaming is consistent across current objects and captured receipts.
    """
    ids = {}
    def identity(number):
        return ids.setdefault(number, len(ids))
    receipts = [o.approval.receipt for o in state.objects if o.approval]
    receipts += [r for _, r in state.slots]
    counters = {}
    for d in ('authority', 'revision', 'latest'):
        values = [getattr(o, d) for o in state.objects] + [getattr(r, d) for r in receipts]
        counters[d] = {n: i for i, n in enumerate(sorted(set(values)))}
    def context(obj, objid):
        return tuple((d, counters[d][getattr(obj, d)] if d in counters else
                      dimension(obj, objid, d)) for d in decl.dimensions)
    def receipt(r):
        return (identity(r.identifier), context(r, r.object))
    objects = tuple((context(o, i), o.state,
                     None if o.approval is None else
                     (receipt(o.approval.receipt), o.approval.positive))
                    for i, o in enumerate(state.objects))
    slots = tuple((channel, receipt(r)) for channel, r in state.slots)
    return (state.selected, objects, slots)


def enabled(state, decl):
    return [(op, result) for op in alphabet(decl) if (result := step(state, op, decl)) is not None]


def target_specs(decl, families=('F1', 'F2', 'F3', 'F4')):
    """Enumerate relation targets and record their required operation capabilities."""
    reasons = [f'state:{s}' for s in decl.states if s not in decl.completion_states]
    reasons += ['dim:' + d for d in decl.dimensions]
    reasons += ['input']
    supported = {'dim:' + d for d, _ in decl.perturb}
    if decl.object_count > 1:
        supported.add('dim:object')
    if decl.identity == 'latest':
        supported.add('dim:latest')
    if 'revision' in decl.dimensions and any(d == 'content' for d, _ in decl.perturb):
        supported.add('dim:revision')
    supported |= {r for r in reasons if r.startswith('state:')}
    if decl.malformed_inputs:
        supported.add('input')
    specs = []
    def add(name, family, obligation, predicate, capability=True):
        specs.append({'name': name, 'family': family, 'obligation': obligation,
                      'capability': capability, 'predicate': predicate})
    if 'F1' in families:
        for reason in reasons:
            for sign in ('pos', 'neg'):
                add(f'preserve[{reason}|{sign}]', 'F1', 'preservation',
                    lambda i, r=reason, s=sign: i.get('complete') and i['sign'] == s
                    and i['valid_before'] and i['why'] == (r,), reason in supported)
            for entry, allowed in [('single', decl.activation_states), ('batch', decl.batch_states)]:
                add(f'admission[{reason}|{entry}]', 'F1', 'admission',
                    lambda i, r=reason, e=entry: i.get('activate') == e and
                    not i['valid_before'] and i.get('refused_positive') == (r,),
                    bool(allowed) and reason in supported and
                    (not reason.startswith('state:') or reason[6:] in allowed))
    if 'F2' in families:
        for entry, allowed in [('single', decl.activation_states), ('batch', decl.batch_states)]:
            for state in allowed:
                add(f'use[{entry}|{state}]', 'F2', 'qualified use',
                    lambda i, e=entry, s=state: i.get('activate') == e and
                    i['eligible'] and i['state_before'] == s)
    if 'F3' in families:
        add('replace[neg]', 'F3', 'applicable completion',
            lambda i: i.get('complete') and i['sign'] == 'neg' and not i['why'] and i['valid_before'])
    if 'F4' in families:
        ops = [op for _, op in decl.perturb] + [op for op, _ in decl.lifecycle]
        ops += ['activate', 'activate_batch', 'request']
        for op in ops:
            add(f'consumed[{op}]', 'F4', 'validity / invalidation',
                lambda i, op=op: ('request' if i['operation'].startswith('request_') else
                                  i['operation']) == op and
                i['state_before'] == decl.consumed_state and i['valid_before'],
                op != 'activate_batch' or bool(decl.batch_states))
    return specs


def synthesize(decl, families=('F1', 'F2', 'F3', 'F4'), max_depth=8, max_witnesses=12):
    """BFS first witness is shortest in the declared bound; witness count is capped."""
    specs = target_specs(decl, families)
    pending = {s['name']: s for s in specs if s['capability']}
    found = {s['name']: [] for s in specs}
    depth = {}
    start = initial(decl)
    frontier = deque([(start, (), None)])
    seen = {(key(start, decl), None): 0}
    transitions = 0
    while frontier:
        state, path, refused = frontier.popleft()
        if len(path) >= max_depth:
            continue
        for op, (next_state, concrete, info) in enabled(state, decl):
            transitions += 1
            last_refused = refused
            if info.get('complete'):
                if info['sign'] == 'pos' and info['why']:
                    last_refused = info['why']
                elif not info['why']:
                    last_refused = None
            info['refused_positive'] = refused
            npath = path + (concrete,)
            for name, spec in list(pending.items()):
                if spec['predicate'](info):
                    depth.setdefault(name, len(npath))
                    if len(npath) == depth[name] and len(found[name]) < max_witnesses:
                        found[name].append(list(npath))
            # Keep target-completion indicators in the quotient, because a
            # refused-positive admission target concerns the preceding delivery.
            k = (key(next_state, decl), last_refused)
            if k not in seen:
                seen[k] = len(npath)
                frontier.append((next_state, npath, last_refused))
        if pending and all(found[n] for n in pending):
            # Each target has a shortest witness; retain the alternatives found so far.
            break
    inventory = []
    for spec in specs:
        name = spec['name']
        status = ('capability_filtered' if not spec['capability'] else
                  'reached' if found[name] else 'bounded_unreached')
        inventory.append({k: v for k, v in spec.items() if k != 'predicate'} |
                         {'status': status, 'depth': depth.get(name), 'witnesses': found[name]})
    return found, {'declaration': asdict(decl), 'max_depth': max_depth,
                   'max_witnesses': max_witnesses, 'states_visited': len(seen),
                   'transitions_examined': transitions, 'targets': inventory}


def compiled_histories(decl, witnesses, seed, budget):
    rng = random.Random(seed); deck = []; rows = []; spent = 0
    names = [name for name, options in witnesses.items() if options]
    if not names:
        return rows
    while spent < budget - 1:
        if not deck:
            deck = list(names); rng.shuffle(deck)
        name = deck.pop(); events = list(rng.choice(witnesses[name]))
        if rng.getrandbits(1):
            events = [event.replace('_a', '_TEMP').replace('_b', '_a').replace('_TEMP', '_b')
                      if event.startswith(('request_', 'complete_')) else event for event in events]
        events = events[:budget - spent - 1]
        rows.append({'obligation': name, 'events': events, 'offset': spent})
        spent += 1 + len(events)
    return rows


def dump_manifest(path, decl, **kwargs):
    _, manifest = synthesize(decl, **kwargs)
    path.write_text(json.dumps(manifest, indent=2) + '\n')
