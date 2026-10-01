"""Replay all effects and summarize the registered public-regression treatments."""
from collections import Counter
from dataclasses import fields
import json
from pathlib import Path
from approval_contract import Contract,OriginRegistry,check_transition

ROOT=Path(__file__).resolve().parents[1]


def main():
    out=ROOT/'results/transfer';summary=[];total_effects=0
    for treatment in ('original','getter_fix','fixed','reverted'):
        for value in (0,4):
            data=json.loads((out/f'{treatment}-{value}.json').read_text())
            contract=data['contract'];contract['invalidations']=tuple((p,tuple(ds)) for p,ds in contract['invalidations'])
            contract=Contract(**contract)
            raw_detected=derived_detected=0
            samples={};findings=Counter()
            for row in data['rows']:
                origins=OriginRegistry();raw=[];derived=[]
                for effect in row['records']:
                    record=effect['record'];total_effects+=1
                    if 'capture' in record:origins.register(record['capture']['id'],record['capture']['context'])
                    result=check_transition(record,origins,contract)
                    assert result==effect['findings']
                    if record['before']['state']=='approved' and record['native_guard_state'] is not None:
                        expected=1 if value==0 and treatment in ('original','reverted') else value
                        assert record['native_guard_state']==expected
                    raw.extend(result);findings.update(result)
                    # Counterfactual replay: substitute the faulty getter's interpretation
                    # for the recorded SQL lifecycle state.
                    bad=json.loads(json.dumps(record))
                    if treatment in ('original','reverted') and value==0:
                        for side in ('before','after'):
                            if bad[side]['state']=='approved':bad[side]['state']='new'
                    derived.extend(check_transition(bad,origins,contract))
                assert sorted(set(raw))==row['findings']
                if row.get('control'):assert not raw
                else:
                    raw_detected+=bool(raw);derived_detected+=bool(derived)
                if row['witness']==0 and row['target'] in ('preserve[state:approved|neg]','use[single|approved]'):
                    samples[row['target']]={'events':row['events'],'findings':row['findings'],
                        'final':row['final'],'last_reported':row['records'][-1]['record']['reported']}
            assert raw_detected==data['detected_histories']
            row={k:data[k] for k in ('treatment','approved_encoding','library_version','generated_histories',
                 'control_histories','detected_histories','admission_detected_histories','control_findings','finding_types')}
            row.update(derived_projection_detections=derived_detected,examples=samples)
            summary.append(row)
    # Outcomes are asserted only after every registered treatment was executed.
    for row in summary:
        failing=row['treatment'] in ('original','reverted') and row['approved_encoding']==0
        assert row['detected_histories']==(16 if failing else 0)
        assert row['admission_detected_histories']==row['control_findings']==0
        assert row['derived_projection_detections']==0
    inventories={r['status']:0 for r in data['inventory']['targets']}
    for r in data['inventory']['targets']:inventories[r['status']]+=1
    shared=('approval_contract','contract_model','causal_schedule','certificate_verifier','specification_coverage','spec_search','matched_search')
    result={'success':True,'source_defects':1,'source':'Viewflow 2.3.1 custom getter zero-state regression',
            'core_modules':list(shared),'inventory_counts':inventories,'conditions':8,
            'generated_histories_total':sum(x['generated_histories'] for x in summary),
            'controls_total':sum(x['control_histories'] for x in summary),
            'replayed_effects':total_effects,'rows':summary,
            'scope':'post-freeze assessment of a public regression in an author-written Django workflow',
            'projection_ablation':'diagnostic recorded-effect replay using the faulty getter interpretation'}
    (out/'analysis.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='rows'},indent=2))
    for row in summary:print(row['treatment'],row['approved_encoding'],row['detected_histories'],row['finding_types'])


if __name__=='__main__':main()
