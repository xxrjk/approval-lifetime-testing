"""Predicate and valuation coverage of the complete step-obligation specification.

The extractor operates on Boolean implications and subexpressions. Its monitor
is checked independently against the effect checker.
"""
from dataclasses import dataclass


def atom(name): return ('atom', name)
def both(*items): return ('and',) + items
def either(*items): return ('or',) + items
def neg(item): return ('not', item)


def evaluate(expr, values):
    op = expr[0]
    if op == 'atom': return values[expr[1]]
    if op == 'not': return not evaluate(expr[1], values)
    results = [evaluate(e, values) for e in expr[1:]]
    return all(results) if op == 'and' else any(results)


def nodes(expr):
    result = {expr}
    if expr[0] != 'atom':
        for child in expr[1:]: result.update(nodes(child))
    return result


@dataclass(frozen=True)
class Obligation:
    name: str
    scope: str
    antecedent: tuple
    consequent: tuple


def obligations(contract):
    old_valid = both(atom('old_known'), atom('old_positive'), atom('old_local'),
                     *(atom('old_bind:'+d) for d in contract.dimensions))
    new_valid = both(atom('new_known'), atom('new_positive'), atom('new_local'),
                     *(atom('new_bind:'+d) for d in contract.dimensions))
    applicable = both(atom('completion_state'), atom('input_valid'), atom('incoming_known'),
                      *(atom('incoming_bind:'+d) for d in contract.dimensions))
    eligible = both(atom('consumer_state'), old_valid)
    refusal = both(neg(applicable), old_valid)
    rules = [
        Obligation('unqualified_admission','activate',both(neg(atom('before_consumed')),atom('after_consumed')),eligible),
        Obligation('active_state_validity','all',atom('after_consumed'),new_valid),
        Obligation('approval_preservation','complete',refusal,both(new_valid,atom('same_old_origin'))),
    ]
    if contract.preserve_state_on_refusal:
        rules.append(Obligation('refusal_state_preservation','complete',refusal,atom('same_state')))
    if contract.liveness:
        rules += [
            Obligation('qualified_refusal','activate',eligible,atom('after_consumed')),
            Obligation('applicable_completion','complete',both(applicable,atom('positive_input')),both(new_valid,atom('same_incoming_origin'))),
            Obligation('applicable_completion','complete',both(applicable,atom('negative_input')),neg(atom('new_positive'))),
        ]
    for prefix, dimensions in contract.invalidations:
        rules.append(Obligation('invalidation_effect','event:'+prefix,atom('true'),
            both(atom('after_reset'),neg(new_valid),*(atom('changed:'+d) for d in dimensions))))
    return rules


def values_for(record, origins, contract):
    before,after = record['before'],record['after']
    old,new = before.get('approval') or {},after.get('approval') or {}
    old_context = origins.get(old.get('origin'))
    new_context = origins.get(new.get('origin'))
    incoming = origins.get(record.get('incoming_origin'))
    sign = record.get('incoming_positive')
    if sign is None:
        event=record.get('event','')
        sign=True if event.endswith('_pos') else False if event.endswith('_neg') else None
    states = contract.batch_states if record.get('entry','single')=='batch' else contract.activation_states
    values = {'true':True, 'old_known':old_context is not None,'new_known':new_context is not None,
              'incoming_known':incoming is not None,'old_positive':bool(old.get('positive')),
              'new_positive':bool(new.get('positive')),'old_local':bool(old.get('local_valid')),
              'new_local':bool(new.get('local_valid')),'input_valid':record.get('incoming_valid',True),
              'positive_input':sign is True,'negative_input':sign is False,
              'completion_state':before['state'] in contract.completion_states,
              'consumer_state':before['state'] in states,
              'before_consumed':before['state']==contract.consumed_state,
              'after_consumed':after['state']==contract.consumed_state,
              'after_reset':after['state']==contract.reset_state,
              'same_state':before['state']==after['state'],
              'same_old_origin':old.get('origin')==new.get('origin'),
              'same_incoming_origin':new.get('origin')==record.get('incoming_origin')}
    for tag,captured,current in [('old',old_context,before['context']),('new',new_context,after['context']),
                                 ('incoming',incoming,before['context'])]:
        for d in contract.dimensions:
            values[tag+'_bind:'+d] = bool(captured is not None and d in captured and d in current and captured[d]==current[d])
    for _,dimensions in contract.invalidations:
        for d in dimensions: values['changed:'+d] = before['context'].get(d)!=after['context'].get(d)
    return values


def active(rule,record):
    return (rule.scope=='all' or record['kind']==rule.scope or
            rule.scope.startswith('event:') and record.get('event','').startswith(rule.scope[6:]))


def monitor(record,origins,contract):
    values=values_for(record,origins,contract)
    return sorted({r.name for r in obligations(contract) if active(r,record)
                   and evaluate(r.antecedent,values) and not evaluate(r.consequent,values)})


def features(record,origins,contract):
    """Extract predicate and valuation targets from every obligation.

    Predicates cover Boolean subexpression truth values within each event scope.
    Valuations cover feasible leaf assignments for each complete implication.
    Identical predicates and rules are deduplicated, including the shared
    preservation antecedent.
    """
    values=values_for(record,origins,contract)
    predicates,cubes=set(),set()
    for rule in obligations(contract):
        if not active(rule,record): continue
        all_nodes=nodes(rule.antecedent)|nodes(rule.consequent)
        for expr in all_nodes:
            if expr != atom('true'):
                predicates.add((rule.scope,expr,evaluate(expr,values)))
        leaves=tuple(sorted(e[1] for e in all_nodes if e[0]=='atom'))
        cubes.add((rule.scope,rule.antecedent,rule.consequent,tuple((n,values[n]) for n in leaves)))
    return {'spec_predicates':predicates,'spec_cubes':cubes}


def model_record(before,after,operation,info,decl):
    """Observation adapter shared by the full monitor and its target extractor."""
    origins={}
    def context(value,objid):
        return {d:objid if d=='object' else getattr(value,d) for d in decl.dimensions}
    for s in (before,after):
        receipts=[r for _,r in s.slots]+[o.approval.receipt for o in s.objects if o.approval]
        for r in receipts:origins[str(r.identifier)]=context(r,r.object)
    def observe(s):
        obj=s.current;a=obj.approval
        return {'context':context(obj,s.selected),'state':obj.state,
                'approval':None if a is None else {'origin':str(a.receipt.identifier),
                           'positive':a.positive,'local_valid':True}}
    record={'before':observe(before),'after':observe(after),'event':operation,'kind':'other'}
    if info.get('complete'):
        record.update(kind='complete',incoming_origin=str(info['incoming']),
                      incoming_valid=info['incoming_valid'],incoming_positive=info['sign']=='pos')
    if info.get('activate'):record.update(kind='activate',entry=info['activate'])
    return record,origins
