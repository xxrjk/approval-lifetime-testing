"""Compare six constructors on a shared approval model and real Dart/SQLite effects.

All constructors receive context bindings, receipt aliases, lifecycle states,
and both consumer entries. Complete 300-unit runs support comparison of the
obligation masks and independent reference postconditions on the same effects.
Each run records the first finding and continues to its execution budget.
"""
from __future__ import annotations
import argparse
from collections import Counter
from dataclasses import asdict
import gzip
import io
import json
import random
import time
from pathlib import Path

from approval_contract import Contract, OriginRegistry, check_transition, SAFETY
from contract_model import Declaration, initial, step, synthesize, compiled_histories
from model_baselines import histories as general_histories
from extension_baseline import rows_for as regression_rows
from rpc_backend import Backend

ROOT = Path(__file__).resolve().parents[1]
STRATEGIES = ('balanced', 'state_coverage', 'edge_coverage', 'pair_coverage',
              'compiled_v4', 'fixed_then_interleaved')
APPLICATION = Contract(('object', 'content', 'authority'),
                       invalidations=(('edit_', ()), ('revoke', ('authority',))))
DECLARATION = Declaration()
ADMISSION = frozenset({'unqualified_admission', 'active_state_validity'})


def reference_difference(model, observation):
    """Compare stored state with independent reference-model postconditions.

    Check lifecycle state and positive approval identity. Positive approvals require
    valid persisted fields; negative diagnostic details are abstracted away.
    """
    expected = model.current
    differences = []
    if observation['state'] != expected.state:
        differences.append('reference_state')
    actual = observation.get('approval')
    positive = bool(actual and actual.get('positive'))
    expected_positive = bool(expected.approval and expected.approval.positive)
    if positive != expected_positive:
        differences.append('reference_positive_effect')
    if expected_positive and positive:
        if actual['origin'] != str(expected.approval.receipt.identifier):
            differences.append('reference_origin')
        if not actual['local_valid']:
            differences.append('reference_local_fields')
    return differences


def events_to_operation(event):
    return 'edit' if event.startswith('edit_') else event


def construct(strategy, seed, budget, declaration=DECLARATION, compiled=None):
    if strategy == 'compiled_v4':
        if compiled is None:
            compiled = synthesize(declaration)[0]
        return compiled_histories(declaration, compiled, seed, budget)
    if strategy == 'fixed_then_interleaved':
        return [{'events': events, 'obligation': strategy} for events in regression_rows(strategy, seed, budget)]
    return general_histories(strategy, declaration, seed, budget)


def execute(backend, rows, strategy, case, seed, budget, writer, construction_seconds):
    run_id = f'{case["variant"]}-{case["field"]}-{strategy}-{seed}'
    first = {'admission': None, 'v2': None, 'v3': None, 'reference': None}
    first_types = {}; first_histories = {}; operations = 0
    totals = Counter(); entry_counts = Counter(); action_counts = Counter()
    valid_uses = 0; target_seconds = observation_seconds = bridge_seconds = checker_seconds = 0.
    started = time.perf_counter(); first_seconds = {}; peak = backend.rss_bytes()
    for history, row in enumerate(rows):
        if operations >= budget:
            break
        operations += 1; call_start = time.perf_counter()
        reset = backend.call({'op': 'reset', 'variant': case['variant'], 'field': case['field']})
        bridge_seconds += time.perf_counter() - call_start
        target_seconds += reset['target_us'] / 1e6
        origins = OriginRegistry(); model = initial(DECLARATION); observation = reset['observation']
        writer.write(json.dumps({'run': run_id, 'history': history, 'reset': True,
                                 'operation': operations, 'observation': observation}) + '\n')
        current_events = []
        for event in row['events']:
            if operations >= budget:
                break
            operations += 1; current_events.append(event); action_counts[event] += 1
            expected = step(model, events_to_operation(event), DECLARATION)
            # Repeating suspension is a legal no-op in fixed histories.
            if expected is None:
                assert event == 'suspend', event
            else:
                model = expected[0]
            call_start = time.perf_counter(); result = backend.call({'op': 'step', 'event': event})
            bridge_seconds += time.perf_counter() - call_start
            target_seconds += result['target_us'] / 1e6
            observation_seconds += result['observation_us'] / 1e6
            record = result['record']; assert record['before'] == observation
            if 'capture' in record:
                origins.register(record['capture']['id'], record['capture']['context'])
            check_start = time.perf_counter()
            findings = check_transition(record, origins, APPLICATION)
            reference = reference_difference(model, record['after'])
            checker_seconds += time.perf_counter() - check_start
            for name, items in [('admission', [f for f in findings if f in ADMISSION]),
                                ('v2', [f for f in findings if f in SAFETY]),
                                ('v3', findings), ('reference', reference)]:
                if items and first[name] is None:
                    first[name] = operations; first_types[name] = items
                    first_histories[name] = list(current_events)
                    first_seconds[name] = time.perf_counter() - started
            if record['kind'] == 'activate':
                entry_counts[record['entry']] += 1
                if record['before']['state'] != 'active' and record['after']['state'] == 'active':
                    valid_uses += 1
            totals.update(findings); observation = record['after']
            writer.write(json.dumps({'run': run_id, 'history': history, 'operation': operations,
                'record': record, 'findings': findings, 'reference_findings': reference,
                'target_us': result['target_us'], 'observation_us': result['observation_us']}) + '\n')
            if operations % 30 == 0:
                peak = max(peak, backend.rss_bytes())
    if case['variant'].endswith('_control'):
        assert not any(first.values()), (run_id, first, first_types)
    return {'run': run_id, 'variant': case['variant'], 'field': case['field'],
            'family': case['family'], 'strategy': strategy, 'seed': seed, 'budget': budget,
            'operations': operations, 'first': first, 'first_types': first_types,
            'first_histories': first_histories, 'first_seconds': first_seconds,
            'finding_counts': dict(totals), 'entry_calls': dict(entry_counts),
            'action_counts': dict(action_counts), 'activations': valid_uses,
            'construction_seconds': construction_seconds, 'total_seconds': time.perf_counter() - started,
            'target_seconds': target_seconds, 'observation_seconds': observation_seconds,
            'bridge_seconds': bridge_seconds, 'checker_seconds': checker_seconds,
            'sampled_peak_combined_rss_bytes': peak}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--flutter', default='flutter'); parser.add_argument('--sqlite-cache')
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--phase', choices=['pilot', 'confirmation'], default='pilot')
    parser.add_argument('--seeds', type=int); parser.add_argument('--budget', type=int, default=300)
    parser.add_argument('--strategies', nargs='+', default=list(STRATEGIES))
    parser.add_argument('--output'); parser.add_argument('--manifest', type=Path)
    parser.add_argument('--app', default='fair_app')
    parser.add_argument('--shard', type=int, default=0)
    parser.add_argument('--shards', type=int, default=1)
    args = parser.parse_args(); out = ROOT/'results/fair'; out.mkdir(parents=True, exist_ok=True)
    cases = json.loads((args.manifest or ROOT/'cases.json').read_text())['included']
    if args.phase == 'pilot' and args.manifest is None:
        cases = [c for c in cases if c['variant'] in ('callback_regression', 'epoch_rebinding',
                  'suspended_negative_write', 'digest_control', 'snapshot_control')]
    count = args.seeds or (2 if args.phase == 'pilot' else None)
    schedule = [(case, strategy, seed) for case in cases for strategy in args.strategies
                for seed in range(16000, 16000 + (count or case.get('seeds', 30)))]
    random.Random(20261001).shuffle(schedule)
    assert 0 <= args.shard < args.shards
    schedule = [item for index, item in enumerate(schedule) if index % args.shards == args.shard]
    t = time.perf_counter(); compiled, inventory = synthesize(DECLARATION)
    compiler_seconds = time.perf_counter() - t
    (out/'application_targets.json').write_text(json.dumps(inventory, indent=2) + '\n')
    cache = {}; prefix = args.output or args.phase
    with Backend(args.flutter, args.sqlite_cache, args.offline, name=args.app) as backend, \
         gzip.open(out/f'{prefix}.observations.jsonl.gz', 'wt', compresslevel=3) as writer, \
         (out/f'{prefix}.runs.jsonl').open('w') as results:
        for index, (case, strategy, seed) in enumerate(schedule):
            if (strategy, seed) not in cache:
                t = time.perf_counter(); rows = construct(strategy, seed, args.budget, compiled=compiled)
                cache[strategy, seed] = (rows, time.perf_counter() - t)
            rows, cost = cache[strategy, seed]
            result = execute(backend, rows, strategy, case, seed, args.budget, writer, cost)
            results.write(json.dumps(result) + '\n'); results.flush()
            if index % 100 == 0:
                print(json.dumps({'completed': index + 1, 'runs': len(schedule), 'run': result['run'],
                                  'first': result['first']}), flush=True)
        startup = backend.startup_seconds
    status = {'success': True, 'phase': args.phase, 'runs': len(schedule),
              'budget': args.budget, 'strategies': args.strategies, 'declaration': asdict(DECLARATION),
              'compiler_seconds': compiler_seconds, 'bridge_startup_seconds': startup,
              'record_all': True, 'history_construction_reused_across_faults': True,
              'execution_partition': {'shard': args.shard, 'shards': args.shards},
              'all_controls_clean': True, 'experimental_model_calls': 0, 'new_human_annotations': 0}
    (out/f'{prefix}.status.json').write_text(json.dumps(status, indent=2) + '\n')
    print(json.dumps(status), flush=True)


if __name__ == '__main__':
    main()
