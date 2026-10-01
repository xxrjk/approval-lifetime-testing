"""Approval contracts evaluated from persisted effects and immutable request origins.

Adapters supply bound context, stored approval, input validity, and before/after
state. The checker evaluates admission, approval preservation, qualified use,
applicable completion, and declared invalidation effects. Admission is triggered
by a persisted transition into the consumed state.
"""
from __future__ import annotations
from dataclasses import dataclass
from collections.abc import Mapping
import json
import random


@dataclass(frozen=True)
class Contract:
    dimensions: tuple[str, ...]
    activation_states: tuple[str, ...] = ('pending', 'suspended')
    batch_states: tuple[str, ...] = ('pending',)
    completion_states: tuple[str, ...] = ('pending',)
    preserve_state_on_refusal: bool = True
    consumed_state: str = 'active'
    reset_state: str = 'pending'
    # Declared invalidation effects: (event prefix, dimensions that must change).
    # An operation matching the prefix must leave the object in reset_state
    # without a valid approval, and must change the named dimensions.
    invalidations: tuple[tuple[str, tuple[str, ...]], ...] = ()
    liveness: bool = True


SAFETY = frozenset({'unqualified_admission', 'active_state_validity',
                    'approval_preservation', 'refusal_state_preservation'})
PRESERVATION = frozenset({'approval_preservation', 'refusal_state_preservation'})
LIVENESS = frozenset({'qualified_refusal', 'applicable_completion'})
EFFECTS = frozenset({'invalidation_effect'})


class OriginRegistry(Mapping):
    """Store immutable value snapshots and return a fresh decoded copy on access."""
    def __init__(self):
        self._encoded = {}

    def register(self, identifier, context):
        if identifier in self._encoded:
            raise ValueError('A request origin cannot be overwritten')
        self._encoded[identifier] = json.dumps(context, sort_keys=True, allow_nan=False)

    def __getitem__(self, identifier):
        return json.loads(self._encoded[identifier])

    def __iter__(self):
        return iter(self._encoded)

    def __len__(self):
        return len(self._encoded)


def context_matches(captured, current, contract):
    return captured is not None and all(
        name in captured and name in current and captured[name] == current[name]
        for name in contract.dimensions)


def may_replace(record, origins, contract):
    """Evaluate completion authority from lifecycle state, input validity, and origin.

    The study supplies valid positive and negative verdicts. Adapters handling
    malformed inputs provide an independently checked `incoming_valid` value.
    Both verdict signs can authorize a replacement.
    """
    before = record['before']
    return (before['state'] in contract.completion_states
            and record.get('incoming_valid', True)
            and context_matches(origins.get(record.get('incoming_origin')),
                                before['context'], contract))


def valid_approval(observation, origins, contract):
    approval = observation.get('approval')
    if not approval or not approval.get('positive') or not approval.get('local_valid'):
        return False
    captured = origins.get(approval.get('origin'))
    if captured is None:
        return False
    return context_matches(captured, observation['context'], contract)


def eligible(observation, origins, contract, entry='single'):
    states = contract.batch_states if entry == 'batch' else contract.activation_states
    return observation['state'] in states and valid_approval(observation, origins, contract)


def released(record, contract):
    """Persisted effect of an activation: the object entered the consumed state."""
    return (record['before']['state'] != contract.consumed_state
            and record['after']['state'] == contract.consumed_state)


def incoming_sign(record):
    """Verdict sign of a completion input, if the adapter declares it."""
    if 'incoming_positive' in record:
        return bool(record['incoming_positive'])
    event = record.get('event', '')
    if event.endswith('_pos'):
        return True
    if event.endswith('_neg'):
        return False
    return None


def check_transition(record, origins, contract):
    """Check the declared obligations against persisted before/after effects."""
    before, after = record['before'], record['after']
    findings = []
    if record['kind'] == 'activate':
        entry = record.get('entry', 'single')
        entered = released(record, contract)
        if entered and not eligible(before, origins, contract, entry):
            findings.append('unqualified_admission')
        if (contract.liveness and not entered
                and eligible(before, origins, contract, entry)):
            findings.append('qualified_refusal')
    if record['kind'] == 'complete':
        if not may_replace(record, origins, contract):
            if valid_approval(before, origins, contract):
                old = before['approval']['origin']
                if (not valid_approval(after, origins, contract) or
                        after['approval']['origin'] != old):
                    findings.append('approval_preservation')
                if (contract.preserve_state_on_refusal and
                        before['state'] != after['state']):
                    findings.append('refusal_state_preservation')
        elif contract.liveness:
            sign = incoming_sign(record)
            approval = after.get('approval')
            if sign is True and not (valid_approval(after, origins, contract)
                                     and approval['origin'] == record.get('incoming_origin')):
                findings.append('applicable_completion')
            if sign is False and approval and approval.get('positive'):
                findings.append('applicable_completion')
    event = record.get('event', '')
    for prefix, changed in contract.invalidations:
        if event.startswith(prefix):
            if (after['state'] != contract.reset_state
                    or valid_approval(after, origins, contract)
                    or any(before['context'].get(d) == after['context'].get(d) for d in changed)):
                findings.append('invalidation_effect')
    if after['state'] == contract.consumed_state and not valid_approval(after, origins, contract):
        findings.append('active_state_validity')
    return findings


def alarm(findings, preservation=True, liveness=True, effects=True):
    """Assertion masks over identical observations."""
    for item in findings:
        if item in PRESERVATION and not preservation:
            continue
        if item in LIVENESS and not liveness:
            continue
        if item in EFFECTS and not effects:
            continue
        return True
    return False


def directed_templates(dimensions=('content','authority')):
    """Instantiate authored history templates using declared invalidation dimensions.

    Each generated history retains the label of its target obligation.
    """
    templates=[('positive_control',['request_a','complete_a_pos','activate']),
               ('negative_control',['request_a','complete_a_neg','activate'])]
    perturb={'content':['edit_b'],'authority':['revoke']}
    for dimension in dimensions:
        change=perturb[dimension]
        templates.append((f'{dimension}_admission',
                          ['request_a',*change,'complete_a_pos','activate']))
        for sign in ['pos','neg']:
            templates.append((f'{dimension}_preservation',
                              ['request_a',*change,'request_b','complete_b_pos',
                               f'complete_a_{sign}','activate']))
    if 'content' in dimensions:
        templates.append(('restored_content_control',
                          ['request_a','edit_b','edit_a','complete_a_pos','activate']))
    if 'authority' in dimensions:
        templates.append(('fresh_authority_control',
                          ['request_a','revoke','request_b','complete_b_pos','activate']))
    templates.extend([
        ('refused_state_preservation',
         ['request_a','complete_a_pos','suspend','complete_a_pos','activate']),
        ('applicable_negative_control',
         ['request_a','request_b','complete_a_pos','complete_b_neg','activate'])])
    return templates


def enabled_random(rng, length=29):
    """Sample enabled action families uniformly while tracking captured receipts."""
    events=[]; captured=set(); value='a'; state='pending'; epoch=0
    packets={}; approval=False
    for _ in range(length):
        families=['request','edit','revoke','activate']
        if captured: families.append('complete')
        if state!='suspended': families.append('suspend')
        kind=rng.choice(families)
        if kind=='request':
            slot=rng.choice(['a','b']); captured.add(slot); packets[slot]=(value,epoch)
            event=f'request_{slot}'
        elif kind=='complete':
            slot=rng.choice(sorted(captured)); sign=rng.choice(['pos','neg'])
            event=f'complete_{slot}_{sign}'
            if state=='pending' and packets[slot]==(value,epoch): approval=sign=='pos'
        elif kind=='edit':
            value='b' if value=='a' else 'a'; event=f'edit_{value}'
            state='pending'; approval=False
        elif kind=='revoke':
            event='revoke'; epoch+=1; state='pending'; approval=False
        elif kind=='suspend':
            event='suspend'; state='suspended'
        else:
            batch=rng.choice([False,True]); event='activate_batch' if batch else 'activate'
            if approval and state in (['pending'] if batch else ['pending','suspended']):
                state='active'
        events.append(event)
    return events


def interleaved(rng):
    """Generic capture-complete-use chains shuffled with lifecycle changes."""
    chains=[['request_a',f'complete_a_{rng.choice(["pos","neg"])}','activate'],
            ['request_b',f'complete_b_{rng.choice(["pos","neg"])}'],
            rng.choice([['edit_b'],['edit_b','edit_a'],['revoke'],['suspend']])]
    result=[]
    while any(chains):
        i=rng.choice([i for i,chain in enumerate(chains) if chain])
        result.append(chains[i].pop(0))
    return result+['activate']


def histories(strategy, seed, budget, dimensions=('content','authority')):
    if strategy=='action_matched':
        # Same per-history action multiset, signs, channels, length and reset
        # positions as the directed constructor. Ablate only its ordering.
        rows=histories('contract',seed,budget,dimensions)
        rng=random.Random(seed+900000)
        for row in rows:
            remaining=list(row['events']);tail=[]
            if remaining and remaining[-1].startswith('activate'):
                tail=[remaining.pop()]
            ordered=[];captured=set()
            while remaining:
                enabled=[i for i,e in enumerate(remaining)
                         if not e.startswith('complete_') or e.split('_')[1] in captured]
                i=rng.choice(enabled);event=remaining.pop(i);ordered.append(event)
                if event.startswith('request_'):captured.add(event.split('_')[1])
            row['events']=ordered+tail
            row['obligation']='action_matched_shuffle'
        return rows
    rng=random.Random(seed)
    rows=[]; spent=0; deck=[]
    while spent < budget-1:
        if strategy=='enabled_random':
            label='random'; events=enabled_random(rng,min(29,budget-spent-1))
        elif strategy=='interleaved':
            label='dependency_interleaving'; events=interleaved(rng)
        elif strategy=='contract':
            if not deck:
                deck=directed_templates(dimensions)
                rng.shuffle(deck)
            label,events=deck.pop()
            events=list(events)
            # Channel renaming and activation entry vary independently of outcomes.
            if rng.choice([False,True]):
                events=[e.replace('_a','_TEMP').replace('_b','_a').replace('_TEMP','_b')
                        if e.startswith(('request_','complete_')) else e for e in events]
            if rng.choice([False,True]):
                events=['activate_batch' if e=='activate' else e for e in events]
        else:
            raise ValueError(strategy)
        events=events[:budget-spent-1]
        if not events: break
        rows.append({'history':len(rows),'obligation':label,'events':events,
                     'offset':spent})
        spent+=1+len(events)
    assert spent<=budget and budget-spent<=1
    return rows
