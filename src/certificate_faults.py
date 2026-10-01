"""Apply prespecified certificate mutations and check acceptance outcomes."""
import copy
import json
from pathlib import Path
from collections import Counter
from certificate_verifier import verify,CertificateRejected

ROOT=Path(__file__).resolve().parents[1]
OPERATORS=('drop_bound_dimension','merge_origins','weaken_native_gate','stale_prestate',
           'swap_completions','drop_dependency','erase_move','forge_applicability','omit_policy_write')


def corrupt(source,operator):
    c=copy.deepcopy(source)
    if operator=='drop_bound_dimension':c['declaration']['dimensions'].pop()
    elif operator=='merge_origins':
        item=c['semantic_projection']['effects'][-1][1]
        names=[x[0] for x in c['semantic_projection']['captures']]
        item['incoming_capture']=next(n for n in names if n!=item['incoming_capture'])
    elif operator=='weaken_native_gate':c['native_capture_states']+=['approved','rejected']
    elif operator=='stale_prestate':
        first=c['semantic_projection']['effects'][0][2]
        c['semantic_projection']['effects'][-1][2]=copy.deepcopy(first)
    elif operator=='swap_completions':
        positions=[i for i,e in enumerate(c['schedule']) if e.startswith('complete_')]
        i,j=positions[:2];c['schedule'][i],c['schedule'][j]=c['schedule'][j],c['schedule'][i]
    elif operator=='drop_dependency':c['dependencies'].pop(0)
    elif operator=='erase_move':
        if not c['swaps']:return None
        c['swaps'].pop(0)
    elif operator=='forge_applicability':
        info=c['semantic_projection']['effects'][-1][1]
        info['why']=[] if info['why'] else ['dim:content']
    elif operator=='omit_policy_write':
        if c['declaration']['identity']!='latest' or not c['swaps']:return None
        for move in c['swaps']:
            move['right_footprint']['writes']=[x for x in move['right_footprint']['writes'] if x not in ('latest','approval')]
    else:raise ValueError(operator)
    return c if c!=source else None


def main():
    original=json.loads((ROOT/'results/causal/validation.json').read_text())
    cases=[r for r in original['rows'] if 'certificate' in r]
    counts=Counter();rows=[];legitimate=0
    for index,row in enumerate(cases):
        source=row['certificate'];decl=copy.deepcopy(source['declaration']);events=list(row['events'])
        assert verify(source,decl,events)
        # Control: vary metadata and edge ordering while preserving certificate semantics.
        control=copy.deepcopy(source);control['comment']='alternate serialization';control['dependencies'].reverse()
        assert verify(control,decl,events);legitimate+=2
        for operator in OPERATORS:
            forged=corrupt(source,operator)
            if forged is None:
                counts[operator+':inapplicable']+=1;continue
            counts[operator+':generated']+=1
            try:verify(forged,decl,events)
            except (CertificateRejected,KeyError,TypeError,IndexError,ValueError) as error:
                outcome='rejected';reason=str(error)
            else:outcome='accepted';reason=''
            counts[operator+':'+outcome]+=1
            rows.append({'source_index':index,'policy':row['policy'],'terminal':row['terminal'],
                         'operator':operator,'outcome':outcome,'reason':reason})
    result={'success':not any(r['outcome']=='accepted' for r in rows),
            'source_histories':len(cases),'legitimate_controls':legitimate,
            'corruptions':len(rows),'counts':dict(counts),'rows':rows,
            'operators_fixed_before_outcomes':True,
            'scope':'certificates from the existing fixed-payload two/three-request model; trusted inputs are separate'}
    (ROOT/'results/certificates/corruptions.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='rows'},indent=2))
    if not result['success']:raise SystemExit(1)


if __name__=='__main__':main()
