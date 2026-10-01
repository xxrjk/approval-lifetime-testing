"""Build a shared BFS catalog for state, edge, pair, and approval targets.

All objectives use the same augmented state keys, transition budget, depth limit,
exploration order, and shortest-witness sampler. Target discovery leaves the
shared catalog search running to its declared stopping condition.
"""
from collections import deque
from dataclasses import asdict
import random
import time

from contract_model import initial, enabled, key, target_specs

KINDS = ('state', 'edge', 'pair', 'approval')


def catalog(decl, seed, max_depth=8, max_transitions=60000, max_witnesses=12):
    start_time = time.perf_counter()
    rng = random.Random(seed)
    start = initial(decl)
    frontier = deque([(start, (), None, None)])
    # Use the same augmented state key for every coverage objective.
    seen = {(key(start, decl), None, None)}
    pools = {kind: {} for kind in KINDS}
    specs = [s for s in target_specs(decl) if s['capability']]
    transitions = 0
    deepest = 0

    def retain(kind, feature, path):
        options = pools[kind].setdefault(feature, [])
        if not options or (len(path) == len(options[0]) and len(options) < max_witnesses):
            options.append(path)

    while frontier and transitions < max_transitions:
        state, path, refused, previous = frontier.popleft()
        if len(path) >= max_depth:
            continue
        options = enabled(state, decl)
        rng.shuffle(options)
        before = key(state, decl)
        for op, (following, concrete, info) in options:
            if transitions == max_transitions:
                break
            transitions += 1
            after = key(following, decl)
            edge = (before, op, after)
            npath = path + (concrete,)
            deepest = max(deepest, len(npath))
            retain('state', after, npath)
            retain('edge', edge, npath)
            if previous is not None:
                retain('pair', (previous, edge), npath)
            info['refused_positive'] = refused
            for spec in specs:
                if spec['predicate'](info):
                    retain('approval', spec['name'], npath)
            next_refused = refused
            if info.get('complete'):
                if info['sign'] == 'pos' and info['why']:
                    next_refused = info['why']
                elif not info['why']:
                    next_refused = None
            marker = (after, next_refused, edge)
            if marker not in seen:
                seen.add(marker)
                frontier.append((following, npath, next_refused, edge))
    metadata = dict(seed=seed, declaration=asdict(decl), max_depth=max_depth,
                    max_transitions=max_transitions, max_witnesses=max_witnesses,
                    transitions=transitions, states=len(seen), deepest=deepest,
                    remaining_frontier=len(frontier),
                    inventories={kind: len(pool) for kind, pool in pools.items()},
                    reached_approval=sorted(pools['approval']),
                    unreached_approval=sorted(s['name'] for s in specs
                                              if s['name'] not in pools['approval']),
                    search_seconds=time.perf_counter() - start_time)
    return pools, metadata


def sample(pool, seed, budget=300):
    """The exact same target-deck and shortest-witness policy for all objectives."""
    rng = random.Random(seed)
    features = list(pool)
    if not features:
        return []
    deck, rows, spent = [], [], 0
    while spent < budget - 1:
        if not deck:
            deck = list(features)
            rng.shuffle(deck)
        feature = deck.pop()
        events = list(rng.choice(pool[feature]))[:budget - spent - 1]
        rows.append({'events': events, 'obligation': 'shared_search', 'offset': spent})
        spent += 1 + len(events)
    return rows
