import copy, json
from pathlib import Path
import pytest
from adstack.config import load, ConfigError
from adstack.planner import reconcile, PlanError
from adstack import cli

ROOT=Path(__file__).parents[1]
ACCOUNT='1234567890'

def config(): return load(ROOT/'examples/klora.yaml')

def empty(): return {'currency':'USD','campaign':None,'budget':None,'groups':[],'keywords':[],'ads':[],'criteria':[]}

def materialized(c):
    """Simulate stable GAQL readback of successfully created resources."""
    s=empty(); p=reconcile(c,s,ACCOUNT)
    names={}
    def replace(value):
        if isinstance(value,str): return names.get(value,value)
        if isinstance(value,list): return [replace(x) for x in value]
        if isinstance(value,dict): return {k:replace(v) for k,v in value.items()}
        return value
    for i,op in enumerate(p['operations'],1):
        kind,op=next(iter(op.items())); obj=op['create']
        if 'resourceName' in obj:
            name=obj['resourceName'];names[name]=name.rsplit('/',1)[0]+'/'+str(1000+i)
        row=replace(copy.deepcopy(obj))
        if kind=='campaignBudgetOperation': s['budget']={**row,'referenceCount':'1','period':'DAILY'}
        elif kind=='campaignOperation': s['campaign']={**row,'biddingStrategyType':'MANUAL_CPC'}
        elif kind=='adGroupOperation': s['groups'].append(row)
        elif kind=='campaignCriterionOperation':
            s['criteria'].append({**row,'type':'KEYWORD' if 'keyword' in row else 'LOCATION','status':'ENABLED','resourceName':f'{row["campaign"].replace("/campaigns/","/campaignCriteria/")}~{i}'})
        elif kind=='adGroupCriterionOperation':
            s['keywords'].append({**row,'resourceName':f'{row["adGroup"].replace("/adGroups/","/adGroupCriteria/")}~{i}'})
        elif kind=='adGroupAdOperation':
            row['ad']['type']='RESPONSIVE_SEARCH_AD';s['ads'].append({**row,'resourceName':f'{row["adGroup"].replace("/adGroups/","/adGroupAds/")}~{i}'})
    for k in ('groups','keywords','ads','criteria'):s[k].sort(key=lambda x:x['resourceName'])
    return s

def test_initial_full_plan_paused_and_exact():
    c=config(); p=reconcile(c,empty(),ACCOUNT)
    assert len(p['operations'])==95
    assert len(c['groups'])==17
    assert sum(len(g['keywords']) for g in c['groups'])==50
    assert not p['blockers'] and not any(p['requires'].values())
    seen=set()
    for op in p['operations']:
        kind,v=next(iter(op.items())); row=v['create']
        for field in ('campaign','adGroup','campaignBudget'):
            if field in row: assert row[field] in seen
        if 'resourceName' in row:
            assert row['resourceName'].rsplit('/',1)[-1] not in {x.rsplit('/',1)[-1] for x in seen}; seen.add(row['resourceName'])
        if kind=='campaignOperation': assert row['status']=='PAUSED' and not row['networkSettings']['targetContentNetwork']
        if kind=='adGroupCriterionOperation': assert row['keyword']['matchType']=='EXACT' and '[' not in row['keyword']['text']

def test_repeat_noop():
    c=config();p=reconcile(c,materialized(c),ACCOUNT);assert not p['operations'] and not p['blockers']

def test_missing_keyword_and_creative_update():
    c=config();s=materialized(c);g=c['groups'][0];s['keywords']=[k for k in s['keywords'] if k['keyword']['text']!='iscape'];g['ad']=copy.deepcopy(g['ad']);g['ad']['responsiveSearchAd']['headlines'][0]['text']='Free Garden Design Tool'
    p=reconcile(c,s,ACCOUNT);assert len(p['operations'])==3;assert any('superseded' in x for x in p['changes'])

def test_prune_explicit():
    c=config();s=materialized(c);c['groups'][0]['keywords'].pop();c['negatives'].pop();p=reconcile(c,s,ACCOUNT);assert len(p['blockers'])==2 and not p['operations'];p=reconcile(c,s,ACCOUNT,True);assert not p['blockers'] and len(p['operations'])==2;assert any('remove' in list(op.values())[0] for op in p['operations'])

def test_activation_and_increase_flags():
    c=config();s=materialized(c);c['status']='ENABLED';c['budget']+=1_000_000;p=reconcile(c,s,ACCOUNT);assert p['requires']=={'allow_enable':True,'allow_budget_increase':True,'allow_live_changes':False}
    s['campaign']['status']='ENABLED';assert reconcile(c,s,ACCOUNT)['requires']['allow_live_changes']
    with pytest.raises(PlanError):reconcile(c,empty(),ACCOUNT)

def test_currency_and_shared_budget_fail_closed():
    c=config();s=materialized(c);s['currency']='EUR'
    with pytest.raises(PlanError):reconcile(c,s,ACCOUNT)
    s['currency']='USD';s['budget']['explicitlyShared']=True
    with pytest.raises(PlanError):reconcile(c,s,ACCOUNT)

def test_duplicate_yaml_and_broad_keyword(tmp_path):
    path=tmp_path/'c.yaml';source=(ROOT/'examples/klora.yaml').read_text();path.write_text(source+'\nversion: 1\n')
    with pytest.raises(ConfigError):load(path)
    path.write_text(source.replace("'[iscape]'","'iscape'",1))
    with pytest.raises(ConfigError):load(path)

def test_apply_validate_only_and_commit_guards(tmp_path,monkeypatch):
    c=config();s=empty();calls=[]
    class Fake:
        def __init__(self,account):pass
        def mutate(self,ops,validate_only):calls.append(validate_only);return {'ok':True}
    monkeypatch.setattr(cli,'Client',Fake);monkeypatch.setattr(cli,'snapshot',lambda *args:s)
    plan=tmp_path/'p.json';receipt=tmp_path/'receipt.json';path=str(ROOT/'examples/klora.yaml');base=['--customer-id',ACCOUNT]
    assert cli.main(base+['plan',path,'--out',str(plan)])==0 and calls==[]
    apply=base+['apply',path,'--plan',str(plan)]
    assert cli.main(apply)==0 and calls==[True]
    assert cli.main(apply+['--commit'])==1 and calls==[True]
    assert cli.main(apply+['--commit','--confirm-account',ACCOUNT,'--receipt',str(receipt)])==0 and calls==[True,True,False]
    assert json.loads(receipt.read_text())['account']==ACCOUNT
    s.update(materialized(c));assert cli.main(apply)==1 and calls==[True,True,False]

def test_commit_gates_live_changes(tmp_path,monkeypatch):
    c=config();s=materialized(c);s['campaign']['status']='ENABLED';s['budget']['amountMicros']='9000000';calls=[]
    class Fake:
        def __init__(self,account):pass
        def mutate(self,ops,validate_only):calls.append(validate_only);return {}
    monkeypatch.setattr(cli,'Client',Fake);monkeypatch.setattr(cli,'snapshot',lambda *args:s)
    plan=tmp_path/'p.json';path=str(ROOT/'examples/klora.yaml');base=['--customer-id',ACCOUNT]
    assert cli.main(base+['plan',path,'--out',str(plan)])==0
    apply=base+['apply',path,'--plan',str(plan),'--commit','--confirm-account',ACCOUNT,'--receipt',str(tmp_path/'r.json')]
    assert cli.main(apply)==1 and calls==[True]
    assert cli.main(apply+['--allow-live-changes','--allow-budget-increase'])==0 and calls==[True,True,False]
