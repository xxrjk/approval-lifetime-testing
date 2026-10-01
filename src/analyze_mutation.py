"""Summarize the mechanical-mutant study under three oracle levels from the same runs."""
from __future__ import annotations
import glob, json, statistics
from collections import Counter, defaultdict
from pathlib import Path
from approval_contract import SAFETY, LIVENESS, EFFECTS

ROOT = Path(__file__).resolve().parents[1]
LEVELS = {'safety': SAFETY, 'liveness': SAFETY | LIVENESS, 'effects': SAFETY | LIVENESS | EFFECTS}
ORDER = ['contract', 'fixed_regression', 'interleaved', 'enabled_random', 'fixed_then_interleaved',
         'compiled', 'compiled_full', 'hypothesis', 'hypothesis_flat']
SHORT = dict(zip(ORDER, ['D', 'F', 'I', 'R', 'F>I', 'CC', 'CCf', 'H', 'Hf']))


def load(set_name):
    recs = {}
    for f in sorted(glob.glob(str(ROOT/'results/mutation'/f'set{set_name}_worker*.jsonl'))):
        for line in open(f):
            r = json.loads(line)
            if r['id'] not in recs or r.get('outcome') == 'ran':
                recs[r['id']] = r
    return recs


def uses_key(runs):
    return {(r['strategy'], r['field'], r['seed']): (r['valid_uses'], r['releases']) for r in runs}


def summarize(set_name, orig):
    recs = load(set_name)
    muts = [r for k, r in sorted(recs.items()) if k != 'ORIG']
    ran = [r for r in muts if r['outcome'] == 'ran']
    stillborn = [r['id'] for r in muts if r['outcome'] == 'stillborn']
    base = uses_key(orig['runs'])
    out = {'mutants': len(muts), 'stillborn': stillborn, 'ran': len(ran), 'levels': {}}
    for level, types in LEVELS.items():
        killed_by = {r['id']: {x['strategy'] for x in r['runs'] if set(x['first_by_type']) & types} for r in ran}
        killable = [m for m, ks in killed_by.items() if ks]
        per = {}
        for s in ORDER:
            firsts = [min(v for t, v in x['first_by_type'].items() if t in types)
                      for r in ran for x in r['runs'] if x['strategy'] == s and set(x['first_by_type']) & types]
            per[SHORT[s]] = {'killed': sum(s in ks for ks in killed_by.values()),
                             'unique': sum(ks == {s} for ks in killed_by.values()),
                             'detecting_runs': len(firsts),
                             'runs': sum(1 for r in ran for x in r['runs'] if x['strategy'] == s),
                             'mean_first_detection': statistics.mean(firsts) if firsts else None}
        out['levels'][level] = {'killable': len(killable), 'per_strategy': per,
                                'killed_by': {m: sorted(SHORT[s] for s in ks) for m, ks in killed_by.items() if ks}}
    finding_mutants = Counter()
    for r in ran:
        for t in {t for x in r['runs'] for t in x['first_by_type']}:
            finding_mutants[t] += 1
    out['mutants_with_finding_type'] = dict(finding_mutants)
    survivors = []
    for r in ran:
        if any(x['first_by_type'] for x in r['runs']):
            continue
        exc = sum(x['sut_exceptions'] for x in r['runs'])
        same = uses_key(r['runs']) == base
        cls = 'SUT exceptions only' if exc else ('no observable difference' if same else 'valid uses differ, no finding')
        survivors.append({'id': r['id'], 'op': r['op'], 'function': r['function'], 'line': r['line'], 'class': cls})
    out['survivors'] = survivors
    out['survivor_classes'] = dict(Counter(s['class'] for s in survivors))
    return out


def main():
    origs = [load(s).get('ORIG') for s in ['A', 'B']]
    orig = next(o for o in origs if o)
    for o in origs:
        if o:
            assert not any(x['first_by_type'] for x in o['runs']), 'finding on unmodified code'
    report = {'original_findings': 0, 'original_runs': len(orig['runs']),
              'A': summarize('A', orig), 'B': summarize('B', orig)}
    (ROOT/'results/mutation/analysis.json').write_text(json.dumps(report, indent=2) + '\n')
    for set_name in ['A', 'B']:
        x = report[set_name]
        print(f"== set {set_name}: {x['mutants']} mutants, {len(x['stillborn'])} stillborn, {x['ran']} ran; survivors {x['survivor_classes']}")
        for level, y in x['levels'].items():
            print(f"  {level:9s} killable {y['killable']:3d} | " + ' '.join(f"{k}={v['killed']}" for k, v in y['per_strategy'].items()))
        print('  finding types:', x['mutants_with_finding_type'])


if __name__ == '__main__':
    main()
