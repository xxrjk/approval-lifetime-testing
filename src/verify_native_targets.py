"""Check that native captures and pre-effect state realize certified targets."""
from dataclasses import fields, asdict
import json
from pathlib import Path
from approval_contract import Contract, OriginRegistry, valid_approval, may_replace
from causal_schedule import compile_schedule
from certificate_verifier import verify
from contract_model import Declaration

ROOT=Path(__file__).resolve().parents[1]


def main():
    data=json.loads((ROOT/'results/causal/native.json').read_text())
    declaration=dict(data['declaration'])
    for field in fields(Declaration):
        value=declaration[field.name]
        if isinstance(value,list):
            declaration[field.name]=tuple(tuple(x) if isinstance(x,list) else x for x in value)
    decl=Declaration(**declaration)
    programs={p['target']:p for p in data['programs']}
    verified=0
    for row in data['rows']:
        program=programs[row['target']]
        if 'abstract_events' not in program:
            continue
        verify(program['certificate'],asdict(decl),program['abstract_events'])
        certificate=compile_schedule(program['abstract_events'],decl)
        assert certificate['schedule']==row['events']
        assert json.loads(json.dumps(certificate)) == program['certificate']
        registry=OriginRegistry()
        for origin,context in row['origins'].items():registry.register(origin,context)
        captures=[r for r in row['records'] if r['kind']=='capture']
        assert all(r['loaded_status']=='in_progress' for r in captures)
        assert all(r['context']==captures[0]['context'] for r in captures)
        effects=[r for r in row['records'] if r['kind']=='complete']
        expected=certificate['semantic_projection']['effects']
        assert len(effects)==len(expected)
        contract=Contract(('object','content','purpose'))
        for observed,abstract in zip(effects,expected):
            info=abstract[1]
            assert observed['event']==abstract[0]
            assert observed['before']['state']==info['state_before']
            assert observed['incoming_valid']==info['incoming_valid']
            assert observed['incoming_positive']==(info['sign']=='pos')
            assert valid_approval(observed['before'],registry,contract)==info['valid_before']
            assert may_replace(observed,registry,contract)==(not info['why'])
            assert observed['before']['context']==captures[0]['context']
            assert observed['other_target_writers_inside_effect']==0
            verified+=1
    result={'success':True,'target_conditions':24,'verified_target_completion_preconditions':verified,
            'checks':['pending capture gate','loaded immutable context','sign and input validity',
                      'effect-time lifecycle','independently valid approval','replacement authority',
                      'single released writer'],
            'real_effects_may_violate_obligations':True}
    (ROOT/'results/causal/targets.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))


if __name__=='__main__':main()
