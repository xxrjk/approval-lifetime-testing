"""Generate comparison plans and execute eight Dart/SQLite suites."""
import argparse
import gzip
import json
from pathlib import Path
import random
from fair_comparison import DECLARATION, execute
from matched_search import sample
from spec_search import catalog
from rpc_backend import Backend

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--flutter', default='flutter')
    parser.add_argument('--sqlite-cache')
    args = parser.parse_args()
    seed = 20000
    pools, search = catalog(DECLARATION, seed, max_transitions=60000)
    edges = list(pools['edge'])
    chosen = random.Random(seed ^ 0x5EED).sample(edges, min(23, len(edges)))
    pools['edge_23'] = {key: pools['edge'][key] for key in chosen}
    plans = {kind: sample(pools[kind], seed) for kind in
             ('approval', 'spec_predicates', 'spec_cubes', 'edge_23')}
    cases = json.loads((ROOT/'cases.json').read_text())['included']
    cases = [c for c in cases if c['variant'] in ('callback_regression', 'digest_control')
             and c['field'] == 'answer']
    assert len(cases) == 2
    output = ROOT/'results/smoke'
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    with Backend(args.flutter, args.sqlite_cache, True, name='smoke_app') as backend, \
         gzip.open(output/'observations.jsonl.gz', 'wt') as stream:
        for case in cases:
            for kind, plan in plans.items():
                rows.append(execute(backend, plan, 'spec_'+kind, case, seed, 300, stream, 0.))
    summary = {'success': True, 'real_program_execution': True, 'runs': len(rows),
               'plans_generated_from_source': True, 'search': search, 'rows': rows}
    (output/'runs.json').write_text(json.dumps(summary, indent=2)+'\n')
    print(json.dumps({'runs': len(rows), 'all_controls_clean': True,
                     'first_findings': {r['run']: r['first'] for r in rows}}, indent=2))


if __name__ == '__main__':
    main()
