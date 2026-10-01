"""General exploration and state, edge, and pair coverage over the shared model.

Constructors receive declared context, receipt aliases, and operations. Their
coverage objectives are defined by states and transitions.
"""
from collections import Counter, deque
import random

from contract_model import initial, enabled, key


def feature(kind, before, operation, after, previous):
    if kind == 'state_coverage':
        return after
    if kind == 'edge_coverage':
        return (before, operation, after)
    if kind == 'pair_coverage':
        return (previous, operation, after)
    raise ValueError(kind)


def choose_coverage(state, previous, decl, kind, counts, rng, lookahead=3):
    options = enabled(state, decl)
    before = key(state, decl)
    scores = [counts[feature(kind, before, operation, key(result[0], decl), previous)]
              for operation, result in options]
    best = min(scores)
    tied = [i for i, score in enumerate(scores) if score == best]
    if best == 0:
        return options[rng.choice(tied)][0]
    # Route to uncovered states, edges, or pairs with shortest-path search.
    rng.shuffle(options)
    frontier = deque([(result[0], (operation,), (before, operation, key(result[0], decl)))
                      for operation, result in options])
    seen = {(key(s, decl), prior) for s, _, prior in frontier}
    while frontier:
        node, path, prior = frontier.popleft()
        if len(path) >= lookahead:
            continue
        node_key = key(node, decl)
        successors = enabled(node, decl); rng.shuffle(successors)
        for operation, result in successors:
            after = key(result[0], decl)
            f = feature(kind, node_key, operation, after, prior)
            if counts[f] == 0:
                return path[0]
            following = (node_key, operation, after)
            marker = (after, following if kind == 'pair_coverage' else None)
            if marker not in seen:
                seen.add(marker)
                frontier.append((result[0], path + (operation,), following))
    fallback = [(op, counts[feature(kind, before, op, key(result[0], decl), previous)])
                for op, result in options]
    least = min(score for _, score in fallback)
    return rng.choice([op for op, score in fallback if score == least])


def histories(kind, decl, seed, budget, length=29):
    """Generate histories using the shared operation set and declared abstraction."""
    rng = random.Random(seed); counts = Counter(); action_counts = Counter()
    rows = []; spent = 0
    while spent < budget - 1:
        state = initial(decl); previous = None; events = []
        for _ in range(min(length, budget - spent - 1)):
            options = enabled(state, decl)
            if kind == 'balanced':
                least = min(action_counts[op] for op, _ in options)
                operation = rng.choice([op for op, _ in options if action_counts[op] == least])
            elif kind == 'uniform_model':
                operation = rng.choice(options)[0]
            else:
                operation = choose_coverage(state, previous, decl, kind, counts, rng)
            result = dict(options)[operation]
            before, after = key(state, decl), key(result[0], decl)
            if kind.endswith('_coverage'):
                counts[feature(kind, before, operation, after, previous)] += 1
            action_counts[operation] += 1
            events.append(result[1]); previous = (before, operation, after); state = result[0]
        rows.append({'obligation': kind, 'events': events, 'offset': spent})
        spent += 1 + len(events)
    return rows
