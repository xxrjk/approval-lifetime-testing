"""Exhaustive small-history validation against a separately coded interpreter."""
from dataclasses import replace
from itertools import permutations, product
from collections import Counter
import json
from pathlib import Path

from causal_schedule import compile_schedule, certify_reordering, LoweringRejected
from contract_model import Declaration

ROOT = Path(__file__).resolve().parents[1]


def reference(events, policy, terminal, native=False):
    # Separate interpreter for lifecycle state, request authority, and effects.
    status, latest, approval = 'pending', 0, None
    requests, captures, effects = {}, {}, []
    def snapshot():
        return (status, latest, approval)
    for event in events:
        fields = event.split('_'); channel = fields[1]
        if fields[0] == 'request':
            if (native or policy == 'latest') and status != 'pending':
                return None
            if policy == 'latest':
                latest += 1; approval = None
            requests[channel] = latest
            captures[channel] = latest
        else:
            if channel not in requests:
                return None
            before = snapshot()
            authorized = status == 'pending' and (policy != 'latest' or requests[channel] == latest)
            if authorized:
                positive = fields[2] == 'pos'
                approval = (channel, positive, requests[channel])
                if terminal:
                    status = 'approved' if positive else 'rejected'
            effects.append((event, authorized, before, snapshot()))
            del requests[channel]
    return (sorted(captures.items()), effects, snapshot())


def programs(count):
    channels = 'abc'[:count]
    captures = tuple('request_'+c for c in channels)
    for signs in product(('pos','neg'), repeat=count):
        completions = tuple(f'complete_{c}_{s}' for c,s in zip(channels, signs))
        for events in permutations(captures + completions):
            if tuple(e for e in events if e.startswith('request_')) != captures:
                continue
            if all(events.index(capture) < events.index(completion)
                   for capture, completion in zip(captures, completions)):
                yield events


def main():
    rows, summary, hoist_summary = [], Counter(), Counter()
    for policy, terminal in product(('value','revision','latest'), (False,True)):
        decl = Declaration(dimensions=('object','content') +
                           (() if policy == 'value' else (policy,)), identity=policy,
            states=('pending','approved','rejected'), completion_states=('pending',),
            activation_states=(), batch_states=(), consumed_state='approved',
            perturb=(), lifecycle=(), channels=('a','b','c'),
            positive_completion_state='approved' if terminal else None,
            negative_completion_state='rejected' if terminal else None,
            consume_receipt_on_completion=True)
        for count in (2,3):
            for events in programs(count):
                expected = reference(events, policy, terminal)
                if expected is not None:
                    hoisted = [e for e in events if e.startswith('request_')] + [e for e in events if e.startswith('complete_')]
                    hoist_expected = reference(hoisted, policy, terminal, native=True)
                    try:
                        certify_reordering(events, hoisted, decl)
                    except LoweringRejected:
                        assert hoist_expected != expected
                        hoist_summary['unsafe_rejected'] += 1
                    else:
                        assert hoist_expected == expected
                        hoist_summary['safe_accepted'] += 1
                try:
                    certificate = compile_schedule(events, decl)
                except LoweringRejected as error:
                    result = 'abstract_disabled' if expected is None else 'lowering_rejected'
                    rows.append(dict(policy=policy, terminal=terminal, events=events,
                                     result=result, reason=str(error)))
                    summary[result] += 1
                    continue
                observed = reference(certificate['schedule'], policy, terminal, native=True)
                assert expected is not None and observed == expected, (policy, terminal, events)
                result = 'accepted_reordered' if certificate['swaps'] else 'accepted_unchanged'
                summary[result] += 1
                rows.append(dict(policy=policy, terminal=terminal, events=events,
                                 result=result, certificate=certificate))
    # The scheduler may retain a legal original schedule, but it must reject an
    # unsafe all-captures-first rewrite of that same latest-policy history.
    assert hoist_summary['unsafe_rejected'] > 0
    native = json.loads((ROOT/'results/causal/native.json').read_text())
    from approval_contract import OriginRegistry, Contract, check_transition
    effects = 0
    for row in native['rows']:
        registry=OriginRegistry()
        for origin, context in row['origins'].items():
            registry.register(origin,context)
        for record in row['records']:
            if record['kind']=='complete':
                assert check_transition(record,registry,Contract(('object','content','purpose'))) == record['findings']
                effects += 1
    result = dict(success=True, enumerated=len(rows), summary=dict(summary),
                  forced_hoisting=dict(hoist_summary), rows=rows,
                  independently_replayed_native_effects=effects,
                  scope='two or three one-shot requests, fixed bound payloads, three policies, two lifecycle semantics')
    (ROOT/'results/causal/validation.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k != 'rows'},indent=2))


if __name__=='__main__':
    main()
