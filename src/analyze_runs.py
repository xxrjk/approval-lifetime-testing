"""Independent record replay, cell summaries and method/oracle attribution."""
from collections import Counter, defaultdict
import csv
import gzip
import json
import math
from pathlib import Path
import statistics

from approval_contract import OriginRegistry, check_transition, SAFETY
from contract_model import initial, step
from fair_comparison import APPLICATION, DECLARATION, ADMISSION, reference_difference, events_to_operation

ROOT = Path(__file__).resolve().parents[1]


def replay(prefix='confirmation'):
    path = ROOT/'results/fair'
    runs = {row['run']: row for line in (path/f'{prefix}.runs.jsonl').open()
            if (row := json.loads(line))}
    first = defaultdict(lambda: {'admission': None, 'v2': None, 'v3': None, 'reference': None})
    per_run = Counter(); resets = Counter(); objects = {}; origins = {}; models = {}; effects = 0
    with gzip.open(path/f'{prefix}.observations.jsonl.gz', 'rt') as stream:
        for line in stream:
            row = json.loads(line); identifier = row['run']; per_run[identifier] += 1; effects += 1
            assert per_run[identifier] == row['operation']
            if row.get('reset'):
                resets[identifier] += 1; origins[identifier] = OriginRegistry()
                models[identifier] = initial(DECLARATION); objects[identifier] = row['observation']
                continue
            record = row['record']; assert objects[identifier] == record['before']
            if 'capture' in record:
                origins[identifier].register(record['capture']['id'], record['capture']['context'])
            transition = step(models[identifier], events_to_operation(record['event']), DECLARATION)
            if transition is not None:
                models[identifier] = transition[0]
            else:
                assert record['event'] == 'suspend'
            findings = check_transition(record, origins[identifier], APPLICATION)
            reference = reference_difference(models[identifier], record['after'])
            assert findings == row['findings'], identifier
            assert reference == row['reference_findings'], identifier
            for name, flagged in [('admission', any(f in ADMISSION for f in findings)),
                                  ('v2', any(f in SAFETY for f in findings)),
                                  ('v3', bool(findings)), ('reference', bool(reference))]:
                if flagged and first[identifier][name] is None:
                    first[identifier][name] = row['operation']
            objects[identifier] = record['after']
    for identifier, run in runs.items():
        assert first[identifier] == run['first'], (identifier, first[identifier], run['first'])
        assert per_run[identifier] == run['operations']
    return list(runs.values()), effects


def wilson(success, n):
    z = 1.959963984540054; p = success/n; denominator = 1 + z*z/n
    center = (p + z*z/(2*n))/denominator
    radius = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))/denominator
    return [max(0, center-radius), min(1, center+radius)]


def analyze(prefix='confirmation'):
    runs, effects = replay(prefix); strategies = sorted({r['strategy'] for r in runs})
    cells = []; summaries = {}
    for strategy in strategies:
        selected = [r for r in runs if r['strategy'] == strategy]
        faults = [r for r in selected if r['family'] != 'control']
        controls = [r for r in selected if r['family'] == 'control']
        costs = {}; family_costs = defaultdict(list); any_count = all_count = 0
        oracle_summary = {}
        for oracle in ('admission', 'v2', 'v3', 'reference'):
            oracle_summary[oracle] = {'detected_runs': sum(r['first'][oracle] is not None for r in faults),
                'mean_truncated_cost': statistics.mean(r['first'][oracle] or r['budget'] for r in faults)}
        for variant in sorted({r['variant'] for r in faults}):
            target = [r for r in faults if r['variant'] == variant]
            count = sum(r['first']['v3'] is not None for r in target)
            any_count += count > 0; all_count += count == len(target)
            cost = statistics.mean(r['first']['v3'] or r['budget'] for r in target)
            costs[variant] = cost; family_costs[target[0]['family']].append(cost)
            cells.append({'strategy': strategy, 'variant': variant, 'family': target[0]['family'],
                          'runs': len(target), 'detected': count, 'wilson_seed_interval': wilson(count, len(target)),
                          'C300': cost, 'reference_detected': sum(r['first']['reference'] is not None for r in target),
                          'reference_C300': statistics.mean(r['first']['reference'] or r['budget'] for r in target)})
        summaries[strategy] = {'variants_at_least_once': any_count, 'variants_all_runs': all_count,
            'C300_equal': statistics.mean(costs.values()),
            'C300_family': statistics.mean(statistics.mean(x) for x in family_costs.values()),
            'oracle_levels': oracle_summary,
            'control_runs': len(controls),
            'control_findings': sum(any(r['first'].values()) for r in controls),
            'control_entry_calls': {name: sum(r['entry_calls'].get(name, 0) for r in controls)
                                    for name in ('single', 'batch')},
            'control_activations': sum(r['activations'] for r in controls),
            'record_all_seconds': sum(r['total_seconds'] for r in selected),
            'target_seconds': sum(r['target_seconds'] for r in selected),
            'observation_seconds': sum(r['observation_seconds'] for r in selected),
            'checker_seconds': sum(r['checker_seconds'] for r in selected),
            'bridge_seconds': sum(r['bridge_seconds'] for r in selected),
            'cold_construction_seconds_median': statistics.median({r['seed']: r['construction_seconds']
                                                                   for r in selected}.values()),
            'peak_combined_rss_bytes': max(r['sampled_peak_combined_rss_bytes'] for r in selected)}
        assert summaries[strategy]['control_findings'] == 0
        assert all(summaries[strategy]['control_entry_calls'].values()), strategy
    result = {'success': True, 'runs': len(runs), 'replayed_operations': effects,
              'cells': cells, 'summary': summaries, 'descriptive_seed_intervals_only': True,
              'no_cross_engine_seed_pairing': True, 'record_all_oracle_masks': True}
    path = ROOT/'results/fair'; (path/f'{prefix}.analysis.json').write_text(json.dumps(result, indent=2)+'\n')
    with (path/f'{prefix}.cells.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(cells[0])); writer.writeheader(); writer.writerows(cells)
    compact = {s: {k: x[k] for k in ('variants_at_least_once', 'variants_all_runs',
                                   'C300_equal', 'C300_family', 'control_findings')}
               for s, x in summaries.items()}
    print(json.dumps({'runs': len(runs), 'replayed_operations': effects, 'summary': compact}, indent=2))
    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(); parser.add_argument('--prefix', default='confirmation')
    analyze(parser.parse_args().prefix)
