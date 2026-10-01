"""Compile approval declarations into relation targets and bounded witnesses.

F1 covers replacement-authority failures by lifecycle state, bound dimension,
and verdict sign. With an independently valid approval, it targets preservation;
positive completions without that approval are followed by a consumer entry to
check admission. F2 covers qualified consumption at each declared entry and
state. F3 covers an applicable negative verdict replacing a positive approval.
F4 covers operations on a consumed object with a valid approval, checking active
validity and declared invalidation effects.

Breadth-first search uses the declared abstract operation model and returns
shortest witnesses within its bounds. Independent checks judge the persisted
effects of executed histories. Unresolved targets retain their search bounds.
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass, replace
import random

STATES = ('pending', 'active', 'suspended')
ALL_FAMILIES = ('F1', 'F2', 'F3', 'F4')


@dataclass(frozen=True)
class Declaration:
    """The part of an executable declaration that the compiler reads."""
    dimensions: tuple = ('content', 'authority')
    completion_states: tuple = ('pending',)
    activation_states: tuple = ('pending', 'suspended')
    batch_states: tuple = ('pending',)
    consumed_state: str = 'active'
    channels: tuple = ('a', 'b')
    perturb: tuple = (('content', 'edit'), ('authority', 'revoke'))   # dimension -> operation
    lifecycle: tuple = (('suspend', 'suspended'),)                     # operation -> state
    perturb_resets: bool = True


@dataclass(frozen=True)
class State:
    value: str = 'a'
    epoch: int = 0
    state: str = 'pending'
    approval: tuple | None = None          # (origin id, positive, value, epoch)
    slots: tuple = ()                      # ((channel, (value, epoch, id)), ...)
    next_id: int = 0
    refused_positive: tuple | None = None  # failing conjuncts of the latest refused positive

    def slot(self, channel):
        return dict(self.slots).get(channel)


def valid(s: State):
    a = s.approval
    return a is not None and a[1] and (a[2], a[3]) == (s.value, s.epoch)


def failing_conjuncts(s: State, channel, decl: Declaration):
    value, epoch, _ = s.slot(channel)
    out = []
    if s.state not in decl.completion_states:
        out.append('state:' + s.state)
    if 'content' in decl.dimensions and value != s.value:
        out.append('dim:content')
    if 'authority' in decl.dimensions and epoch != s.epoch:
        out.append('dim:authority')
    return tuple(out)


def alphabet(decl: Declaration):
    ops = []
    for ch in decl.channels:
        ops += [f'request_{ch}', f'complete_{ch}_pos', f'complete_{ch}_neg']
    ops += [op for _, op in decl.perturb] + [op for op, _ in decl.lifecycle]
    return ops + ['activate', 'activate_batch']


def step(s: State, op: str, decl: Declaration):
    """Abstract effect of an operation: (next state, concrete event, info) or None."""
    info = {'op': 'request' if op.startswith('request_') else op,
            'state_before': s.state, 'valid_before': valid(s)}
    if op.startswith('request_'):
        ch = op.split('_')[1]
        slots = dict(s.slots); slots[ch] = (s.value, s.epoch, s.next_id)
        return replace(s, slots=tuple(sorted(slots.items())), next_id=s.next_id + 1), op, info
    if op.startswith('complete_'):
        _, ch, sign = op.split('_')
        if s.slot(ch) is None:
            return None
        why = failing_conjuncts(s, ch, decl)
        info.update(complete=True, sign=sign, why=why)
        if not why:
            value, epoch, ident = s.slot(ch)
            return replace(s, approval=(ident, sign == 'pos', value, epoch), refused_positive=None), op, info
        return replace(s, refused_positive=why if sign == 'pos' else s.refused_positive), op, info
    for dim, name in decl.perturb:
        if op == name:
            if dim == 'content':
                nv = 'b' if s.value == 'a' else 'a'
                ns = replace(s, value=nv); event = f'edit_{nv}'
            else:
                ns = replace(s, epoch=s.epoch + 1); event = name
            if decl.perturb_resets:
                ns = replace(ns, state='pending', approval=None)
            return ns, event, info
    for name, target in decl.lifecycle:
        if op == name:
            if s.state == target:
                return None
            return replace(s, state=target), name, info
    if op in ('activate', 'activate_batch'):
        entry = 'batch' if op == 'activate_batch' else 'single'
        allowed = decl.batch_states if entry == 'batch' else decl.activation_states
        info.update(activate=entry, eligible=valid(s) and s.state in allowed,
                    refused_positive=s.refused_positive)
        return (replace(s, state=decl.consumed_state) if info['eligible'] else s), op, info
    raise ValueError(op)


def targets(decl: Declaration, families=ALL_FAMILIES):
    """(name, family, obligation, predicate over the final operation's info)."""
    out = []
    reasons = [f'state:{st}' for st in STATES if st not in decl.completion_states]
    reasons += [f'dim:{d}' for d in decl.dimensions]
    if 'F1' in families:
        for why in reasons:
            for sign in ('pos', 'neg'):
                out.append((f'preserve[{why}|{sign}]', 'F1', 'preservation',
                            lambda i, why=why, sign=sign: i.get('complete') and i['sign'] == sign
                            and i['valid_before'] and i['why'] == (why,)))
        for why in reasons:
            for entry, allowed in (('single', decl.activation_states), ('batch', decl.batch_states)):
                if why.startswith('state:') and why[6:] not in allowed:
                    continue
                out.append((f'admission[{why}|{entry}]', 'F1', 'admission',
                            lambda i, why=why, entry=entry: i.get('activate') == entry
                            and not i['valid_before'] and i['refused_positive'] == (why,)))
    if 'F2' in families:
        for entry, allowed in (('single', decl.activation_states), ('batch', decl.batch_states)):
            for st in allowed:
                out.append((f'use[{entry}|{st}]', 'F2', 'qualified use',
                            lambda i, entry=entry, st=st: i.get('activate') == entry
                            and i['eligible'] and i['state_before'] == st))
    if 'F3' in families:
        out.append(('replace[neg]', 'F3', 'applicable completion',
                    lambda i: i.get('complete') and i['sign'] == 'neg' and not i['why'] and i['valid_before']))
    if 'F4' in families:
        ops = [op for _, op in decl.perturb] + [op for op, _ in decl.lifecycle]
        ops += ['activate', 'activate_batch', 'request']
        for op in ops:
            out.append((f'consumed[{op}]', 'F4', 'active validity / invalidation effect',
                        lambda i, op=op: i['op'] == op and i['state_before'] == decl.consumed_state
                        and i['valid_before']))
    return out


def synthesize(decl: Declaration, families=ALL_FAMILIES, max_depth=8, max_witnesses=12):
    """All shortest witnesses per target (bounded), by BFS over the abstract model."""
    tgt = targets(decl, families)
    found = {name: [] for name, *_ in tgt}
    depth = {}
    seen = {State(): 0}
    frontier = deque([(State(), [])])
    ops = alphabet(decl)
    while frontier:
        s, path = frontier.popleft()
        if len(path) >= max_depth:
            continue
        for op in ops:
            r = step(s, op, decl)
            if r is None:
                continue
            ns, event, info = r
            npath = path + [event]
            for name, _, _, pred in tgt:
                if pred(info):
                    d = depth.setdefault(name, len(npath))
                    if len(npath) == d and len(found[name]) < max_witnesses:
                        found[name].append(npath)
            if ns not in seen or seen[ns] == len(npath):
                if ns not in seen:
                    seen[ns] = len(npath)
                frontier.append((ns, npath))
    meta = {name: (family, obligation) for name, family, obligation, _ in tgt}
    return found, meta


def compiled_histories(decl: Declaration, witnesses, seed, budget):
    """Shuffled deck over reachable targets; random shortest witness; channel renaming."""
    rng = random.Random(seed)
    names = [n for n, w in witnesses.items() if w]
    rows = []; spent = 0; deck = []
    while spent < budget - 1:
        if not deck:
            deck = list(names); rng.shuffle(deck)
        name = deck.pop()
        events = list(rng.choice(witnesses[name]))
        if rng.choice([False, True]):
            events = [e.replace('_a', '_TEMP').replace('_b', '_a').replace('_TEMP', '_b')
                      if e.startswith(('request_', 'complete_')) else e for e in events]
        events = events[:budget - spent - 1]
        if not events:
            break
        rows.append({'history': len(rows), 'obligation': name, 'events': events, 'offset': spent})
        spent += 1 + len(events)
    assert spent <= budget and budget - spent <= 1
    return rows


if __name__ == '__main__':
    import json
    for label, decl in [('application', Declaration()),
                        ('completion also while suspended', Declaration(completion_states=('pending', 'suspended'))),
                        ('content-only consumer', Declaration(dimensions=('content',), perturb=(('content', 'edit'),),
                                                              lifecycle=(), activation_states=('pending',)))]:
        found, meta = synthesize(decl)
        print(f'== {label}: {sum(bool(w) for w in found.values())}/{len(found)} targets reached')
        for name, ws in found.items():
            family, obligation = meta[name]
            print(f'  {family} {name:34s} {obligation:38s} depth={len(ws[0]) if ws else "-"} e.g. {ws[0] if ws else "UNREACHED"}')
