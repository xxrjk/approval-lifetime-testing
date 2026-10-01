"""Execute the shared-search coverage comparison on Dart/SQLite."""
import argparse
import gzip
import json
from pathlib import Path
import random
import time

from fair_comparison import DECLARATION, execute
from matched_search import KINDS, catalog, sample
from rpc_backend import Backend

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=['pilot', 'confirmation'], default='pilot')
    parser.add_argument('--transitions', type=int, default=60000)
    parser.add_argument('--flutter', default='flutter')
    parser.add_argument('--sqlite-cache')
    args = parser.parse_args()
    out = ROOT/'results/matched'
    cases = json.loads((ROOT/'cases.json').read_text())['included']
    first_seed, seed_count = (17000, 2) if args.phase == 'pilot' else (18000, 30)
    if args.phase == 'pilot':
        cases = [c for c in cases if c['variant'] in
                 ('callback_regression', 'epoch_rebinding', 'suspended_negative_write', 'digest_control')]
    plans, searches = {}, []
    for seed in range(first_seed, first_seed + seed_count):
        pools, search = catalog(DECLARATION, seed, max_transitions=args.transitions)
        searches.append(search)
        for kind in KINDS:
            started = time.perf_counter()
            rows = sample(pools[kind], seed)
            plans[kind, seed] = (rows, time.perf_counter() - started)
        print(json.dumps({'catalog': seed, **{k: search[k] for k in
                         ('transitions', 'deepest', 'inventories', 'search_seconds')}}), flush=True)
    (out/f'{args.phase}.search.json').write_text(json.dumps(searches, indent=2)+'\n')
    (out/f'{args.phase}.plans.json').write_text(json.dumps([
        {'kind': kind, 'seed': seed, 'rows': value[0], 'selection_seconds': value[1]}
        for (kind, seed), value in plans.items()])+'\n')
    schedule = [(case, kind, seed) for case in cases for kind in KINDS
                for seed in range(first_seed, first_seed +
                                  (seed_count if args.phase == 'pilot' else case.get('seeds', 30)))]
    random.Random(20261002).shuffle(schedule)
    # Write the shared observation format under this comparison's prefix.
    prefix = 'matched_' + args.phase
    with Backend(args.flutter, args.sqlite_cache, True, name='matched_app') as backend, \
         gzip.open(ROOT/f'results/fair/{prefix}.observations.jsonl.gz', 'wt', compresslevel=3) as writer, \
         (ROOT/f'results/fair/{prefix}.runs.jsonl').open('w') as stream:
        for index, (case, kind, seed) in enumerate(schedule):
            rows, cost = plans[kind, seed]
            result = execute(backend, rows, 'matched_' + kind, case, seed, 300, writer, cost)
            stream.write(json.dumps(result)+'\n'); stream.flush()
            if index % 100 == 0:
                print(json.dumps({'completed': index+1, 'runs': len(schedule), 'first': result['first']}), flush=True)
    (out/f'{args.phase}.status.json').write_text(json.dumps(
        {'success': True, 'runs': len(schedule), 'searches': len(searches),
         'phase': args.phase, 'budget': 300, 'same_catalog_for_all_objectives': True,
         'only_varied_component': 'target_projection', 'record_all': True}, indent=2)+'\n')


if __name__ == '__main__':
    main()
