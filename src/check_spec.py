"""Compare the complete Boolean monitor with the effect checker on recorded effects."""
import argparse
import gzip
import json
from pathlib import Path
from approval_contract import OriginRegistry,check_transition
from fair_comparison import APPLICATION,DECLARATION
from specification_coverage import monitor,obligations

ROOT=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser();p.add_argument('--prefix',default='spec_confirmation');a=p.parse_args()
    registries={};count=0;violations=0
    with gzip.open(ROOT/f'results/fair/{a.prefix}.observations.jsonl.gz','rt') as stream:
        for line in stream:
            row=json.loads(line);run=row['run']
            if row.get('reset'):
                registries[run]=OriginRegistry();continue
            r=row['record'];registry=registries[run]
            if 'capture' in r:registry.register(r['capture']['id'],r['capture']['context'])
            actual=monitor(r,registry,APPLICATION)
            expected=sorted(set(check_transition(r,registry,APPLICATION)))
            if actual!=expected:raise AssertionError((run,row['operation'],actual,expected))
            count+=1;violations+=bool(actual)
    result={'success':True,'effects':count,'effects_with_findings':violations,
            'all_obligation_findings_equal':True,'rule_count':len(obligations(APPLICATION))}
    (ROOT/f'results/spec/{a.prefix}.monitor.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


if __name__=='__main__':main()
