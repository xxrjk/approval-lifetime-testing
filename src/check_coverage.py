"""Measure constructor behavior on regenerated model histories."""
from collections import Counter
import json
from pathlib import Path
from contract_model import initial, step, target_specs
from fair_comparison import DECLARATION, construct, events_to_operation

ROOT=Path(__file__).resolve().parents[1]


def audit(rows):
    counts=Counter(); targets=set(); specs=target_specs(DECLARATION)
    for row in rows:
        counts['resets']+=1; state=initial(DECLARATION); refused=None
        consecutive=0
        for event in row['events']:
            result=step(state,events_to_operation(event),DECLARATION)
            if result is None:
                continue
            following,_,info=result
            info['refused_positive']=refused
            for spec in specs:
                if spec['predicate'](info):
                    targets.add(spec['name'])
            counts['actions']+=1
            if event.startswith('request_'):
                counts['captures']+=1;consecutive+=1
                counts['longest_capture_streak']=max(counts['longest_capture_streak'],consecutive)
            else:consecutive=0
            if info.get('complete'):
                counts['completions']+=1
                counts['applicable_completions']+=not info['why']
                counts['preservation_opportunities']+=bool(info['why'] and info['valid_before'])
                if info['sign']=='pos' and info['why']:refused=info['why']
                elif not info['why']:refused=None
            if info.get('activate'):
                counts['use_attempts']+=1;counts['eligible_uses']+=info['eligible']
            state=following
    return dict(counts)|{'targets_reached':len(targets),'targets':sorted(targets)}


def main():
    rows=[]
    for name in ('state_coverage','edge_coverage','pair_coverage','compiled_v4'):
        for seed in range(16000,16030):
            rows.append({'constructor':name,'seed':seed,**audit(construct(name,seed,300))})
    plans=json.loads((ROOT/'results/matched/confirmation.plans.json').read_text())
    for plan in plans:
        rows.append({'constructor':'matched_'+plan['kind'],'seed':plan['seed'],**audit(plan['rows'])})
    summary={}
    for name in sorted({r['constructor'] for r in rows}):
        selected=[r for r in rows if r['constructor']==name]
        summary[name]={k:sum(r.get(k,0) for r in selected)/len(selected)
                       for k in ('resets','actions','captures','completions','applicable_completions',
                                 'preservation_opportunities','use_attempts','eligible_uses','targets_reached')}
        summary[name]['max_capture_streak']=max(r.get('longest_capture_streak',0) for r in selected)
    result={'success':True,'summary':summary,'rows':rows,
            'scope':'constructor behavior in frozen reference model, not additional live fault runs'}
    (ROOT/'results/matched/behavior.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(summary,indent=2))


if __name__=='__main__':main()
