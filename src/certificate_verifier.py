"""Independently verify certificates for one-shot capture/completion histories.

The declaration, original witness, and native capture gate are trusted inputs.
A separate interpreter replays the fixed-context language and rejects unsupported
events or state models.
"""
import json


class CertificateRejected(ValueError): pass


def require(condition,message):
    if not condition: raise CertificateRejected(message)


def normal(value): return json.loads(json.dumps(value,allow_nan=False))


def event_parts(event,decl):
    parts=event.split('_')
    require(len(parts) in (2,3),'event syntax')
    require(parts[1] in decl['channels'],'undeclared request channel')
    if parts[0]=='request':
        require(len(parts)==2,'capture syntax');return parts
    require(parts[0]=='complete' and len(parts)==3,'unregistered event')
    signs={'pos','neg'} | ({'badpos','badneg'} if decl['malformed_inputs'] else set())
    require(parts[2] in signs,'undeclared verdict')
    return parts


def interpret(events,decl,native_gate=None):
    require(decl['object_count'] in (1,2),'unsupported object model')
    known={'object','content','authority','revision','latest','purpose'}
    require(set(decl['dimensions']) <= known,'unsupported context dimension')
    require(decl['identity'] in ('value','revision','latest'),'unsupported identity')
    objects=[dict(content='a',authority=0,revision=0,latest=0,purpose='review',
                  state=decl['reset_state'],approval=None) for _ in range(decl['object_count'])]
    receipts={};done=set();captures={};effects=[]
    def context(value,objid):
        return [[d,objid if d=='object' else value[d]] for d in decl['dimensions']]
    def valid(obj,objid):
        a=obj['approval']
        return bool(a and a['positive'] and a['context']==context(obj,objid))
    def projection():
        rows=[]
        for i,obj in enumerate(objects):
            a=obj['approval']
            rows.append([context(obj,i),obj['state'],None if a is None else
                         [a['origin'],a['context'],a['positive']]])
        return [0,rows]
    for event in events:
        parts=event_parts(event,decl);channel=parts[1];obj=objects[0]
        if parts[0]=='request':
            require(channel not in receipts and channel not in done,'reused live channel')
            if native_gate is not None:require(obj['state'] in native_gate,'native capture disabled')
            if decl['identity']=='latest':
                require(obj['state'] in decl['completion_states'],'latest capture disabled')
                obj['latest']+=1;obj['approval']=None
            receipts[channel]={'origin':event,'context':context(obj,0)}
            captures[event]=context(obj,0)
            continue
        require(channel in receipts and channel not in done,'uncaptured or repeated completion')
        receipt=receipts[channel];sign=parts[2];positive=sign.endswith('pos');input_valid=not sign.startswith('bad')
        reasons=[]
        if obj['state'] not in decl['completion_states']:reasons.append('state:'+obj['state'])
        if not input_valid:reasons.append('input')
        old_context=dict(receipt['context'])
        for d,value in context(obj,0):
            if old_context[d]!=value:reasons.append('dim:'+d)
        facts={'operation':event,'state_before':obj['state'],'valid_before':valid(obj,0),
               'object':0,'complete':True,'sign':'pos' if positive else 'neg',
               'incoming_valid':input_valid,'why':reasons,'incoming_capture':receipt['origin']}
        before=normal(projection())
        if not reasons:
            obj['approval']={'origin':receipt['origin'],'context':receipt['context'],'positive':positive}
            destination=decl['positive_completion_state'] if positive else decl['negative_completion_state']
            if destination:obj['state']=destination
        effects.append([event,facts,before,normal(projection())]);done.add(channel)
    require(done==set(receipts),'incomplete native request')
    return {'captures':[[n,captures[n]] for n in sorted(captures)],'effects':effects,'final':projection()}


def expected_footprint(event,decl):
    if event.startswith('request_'):
        writes=['request_slot','fresh_identity']
        if decl['identity']=='latest':writes+=['latest','approval']
        return dict(reads=decl['dimensions'],writes=writes)
    return dict(reads=decl['dimensions']+['state','approval','request_origin'],
                writes=['approval','state']+(['request_slot'] if decl['consume_receipt_on_completion'] else []))


def verify(certificate,trusted_declaration,trusted_original,trusted_native_gate=('pending',)):
    c=normal(certificate);decl=normal(trusted_declaration);original=list(trusted_original)
    require(c['declaration']==decl,'declaration differs from trusted input')
    require(c['original']==original,'original target witness changed')
    require(c['native_capture_states']==list(trusted_native_gate),'native gate changed')
    require(c['native_route_enabled'] is True,'missing route check')
    schedule=c['schedule']
    require(sorted(schedule)==sorted(original),'event multiset changed')
    for prefix in ('request_','complete_'):
        require([e for e in schedule if e.startswith(prefix)]==[e for e in original if e.startswith(prefix)],'required event order changed')
    captures=[e for e in original if e.startswith('request_')]
    completes=[e for e in original if e.startswith('complete_')]
    required=set(zip(captures,captures[1:])) | set(zip(completes,completes[1:]))
    required|={(f'request_{e.split("_")[1]}',e) for e in completes}
    edges={tuple(e) for e in c['dependencies']}
    require(required<=edges,'missing required dependency')
    require(all(a in schedule and b in schedule and schedule.index(a)<schedule.index(b) for a,b in edges),'invalid dependency order')
    reference=interpret(original,decl)
    require(c['semantic_projection']==reference,'forged semantic projection')
    current=list(original)
    for move in c['swaps']:
        prefix=move['prefix'];i=len(prefix)
        require(current[:i]==prefix,'stale move prefix')
        require(current[i:i+2]==[move['left'],move['right']],'crossed events differ')
        require(move['left'].startswith('complete_') and move['right'].startswith('request_'),'unsupported movement')
        for k in ('capture_contexts_equal','authority_facts_equal','protected_effects_equal','abstract_events_enabled'):
            require(move[k] is True,'failed move predicate')
        for name,event in (('left_footprint',move['left']),('right_footprint',move['right'])):
            expected=expected_footprint(event,decl)
            require(all(set(move[name][key])==set(expected[key]) for key in ('reads','writes')),'incorrect footprint')
        current[i:i+2]=[move['right'],move['left']]
        require(interpret(current,decl)==reference,'movement changes authority/effects')
    require(current==schedule,'moves do not produce schedule')
    require(interpret(schedule,decl,list(trusted_native_gate))==reference,'native realization changes target')
    return True
