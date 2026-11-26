"""Pure, deterministic desired-state reconciliation. No network or side effects."""
from __future__ import annotations
import json, hashlib
from collections import defaultdict

class PlanError(ValueError):
    pass

NETWORK = {'targetGoogleSearch':True,'targetSearchNetwork':False,'targetContentNetwork':False,'targetPartnerSearchNetwork':False}

def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

def key(keyword):
    return (' '.join(keyword.get('text','').lower().split()),keyword.get('matchType'))

def creative(ad):
    rsa = ad.get('responsiveSearchAd',{})
    def assets(field):
        return sorted((a.get('text',''),a.get('pinnedField','UNSPECIFIED')) for a in rsa.get(field,[]))
    return (tuple(sorted(ad.get('finalUrls',[]))),assets('headlines'),assets('descriptions'),rsa.get('path1',''),rsa.get('path2',''))

def reconcile(config, state, account, prune=False):
    if state['currency'] != config['currency']:
        raise PlanError(f"Account currency is {state['currency']}, not {config['currency']}")
    ops, changes, blockers = [], [], []
    temp = 0
    enable = increase = False
    current = state['campaign']
    base = f'customers/{account}'
    def rn(collection):
        nonlocal temp
        temp -= 1
        return f'{base}/{collection}/{temp}'
    def add(kind, action, fields, description, mask=None):
        op = {action:fields}
        if mask: op['updateMask'] = ','.join(mask)
        ops.append({kind+'Operation':op}); changes.append(description)
    def update(kind, resource, fields, description, mask=None):
        add(kind,'update',{'resourceName':resource,**fields},description,mask or list(fields))
    def unique(rows, fn, description):
        result = {}
        for row in rows:
            k = fn(row)
            if k in result: raise PlanError(f'Duplicate {description}: {k}')
            result[k] = row
        return result
    def pause(kind, row, description):
        if row.get('status') != 'PAUSED':
            update(kind,row['resourceName'],{'status':'PAUSED'},description)
    def extra(kind, row, description, remove=False):
        if not prune:
            blockers.append(description+'; re-plan with --prune to '+('remove' if remove else 'pause'))
        elif remove:
            add(kind,'remove',row['resourceName'],description+' (remove)')
        else:
            pause(kind,row,description+' (pause)')
    if not current:
        if config['status'] != 'PAUSED':
            raise PlanError('New campaigns must be PAUSED. Create first, then enable in a separate plan.')
        budget_rn = rn('campaignBudgets')
        campaign_rn = rn('campaigns')
        add('campaignBudget','create',{'resourceName':budget_rn,'name':config['name']+':budget','amountMicros':str(config['budget']),'deliveryMethod':'STANDARD','explicitlyShared':False},f"Create dedicated daily budget {config['currency']} {config['budget']/1e6:.2f}")
        add('campaign','create',{'resourceName':campaign_rn,'name':config['name'],'status':'PAUSED','advertisingChannelType':'SEARCH','campaignBudget':budget_rn,'manualCpc':{},'networkSettings':NETWORK,'containsEuPoliticalAdvertising':'DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING','geoTargetTypeSetting':{'positiveGeoTargetType':'PRESENCE'}},f"Create PAUSED search campaign {config['name']}")
    else:
        if current.get('advertisingChannelType')!='SEARCH' or current.get('biddingStrategyType')!='MANUAL_CPC':
            raise PlanError('Existing named campaign must be SEARCH with MANUAL_CPC; refusing to adopt or change strategy')
        if current.get('status') not in ('PAUSED','ENABLED'):
            raise PlanError('Unexpected current campaign status')
        budget = state['budget']
        if budget.get('explicitlyShared') or int(budget.get('referenceCount',0))>1 or budget.get('period')!='DAILY':
            raise PlanError('Existing campaign budget must be dedicated, not referenced by multiple campaigns, and daily')
        campaign_rn = current['resourceName']
        old_budget = int(budget['amountMicros'])
        if old_budget != config['budget']:
            increase = config['budget'] > old_budget
            update('campaignBudget',budget['resourceName'],{'amountMicros':str(config['budget'])},f"Daily budget {old_budget/1e6:.2f} -> {config['budget']/1e6:.2f} {config['currency']}")
        network = current.get('networkSettings',{})
        changed = {k:v for k,v in NETWORK.items() if network.get(k,False)!=v}
        if changed:
            update('campaign',campaign_rn,{'networkSettings':changed},'Restore Google Search only network settings',['networkSettings.'+k for k in changed])
        if current.get('geoTargetTypeSetting',{}).get('positiveGeoTargetType')!='PRESENCE':
            update('campaign',campaign_rn,{'geoTargetTypeSetting':{'positiveGeoTargetType':'PRESENCE'}},'Limit geographic targeting to presence',['geoTargetTypeSetting.positiveGeoTargetType'])
        if current['status'] != config['status']:
            enable = config['status']=='ENABLED'
            update('campaign',campaign_rn,{'status':config['status']},f"Campaign {current['status']} -> {config['status']}")
    # Geographic and campaign negative keyword criteria. Never introduce unrestricted geo targeting.
    locations, negatives = {}, {}
    for c in state['criteria']:
        if c.get('type')=='LOCATION' and not c.get('negative',False):
            k = c['location']['geoTargetConstant'].split('/')[-1]
            if k in locations: raise PlanError('Duplicate campaign location')
            locations[k]=c
        elif c.get('type')=='KEYWORD' and c.get('negative',False):
            k=key(c['keyword'])
            if k in negatives: raise PlanError('Duplicate campaign negative keyword')
            negatives[k]=c
        else:
            blockers.append('Unmanaged campaign criterion: '+c['resourceName']+' (review manually)')
    for l in config['locations']:
        if l not in locations:
            add('campaignCriterion','create',{'campaign':campaign_rn,'location':{'geoTargetConstant':'geoTargetConstants/'+l}},f'Add location {l}')
        elif locations[l].get('status')=='PAUSED':
            update('campaignCriterion',locations[l]['resourceName'],{'status':'ENABLED'},f'Enable location {l}')
    for k,c in locations.items():
        if k not in config['locations']: extra('campaignCriterion',c,f'Extra location {k}',True)
    desired_negative = {key(x):x for x in config['negatives']}
    for k,n in desired_negative.items():
        if k not in negatives:
            add('campaignCriterion','create',{'campaign':campaign_rn,'negative':True,'keyword':n},f'Add negative {k[1]}: {k[0]}')
        elif negatives[k].get('status')=='PAUSED':
            blockers.append(f'Paused negative keyword {k[0]}; review manually')
    for k,c in negatives.items():
        if k not in desired_negative: extra('campaignCriterion',c,f'Extra campaign negative {k[1]}: {k[0]}',True)
    groups = unique(state['groups'],lambda g:g['name'],'ad group name')
    keywords, ads = defaultdict(list), defaultdict(list)
    for k in state['keywords']: keywords[k['adGroup']].append(k)
    for a in state['ads']: ads[a['adGroup']].append(a)
    for g in config['groups']:
        name=g['name']; old=groups.get(name)
        if old is None:
            group_rn=rn('adGroups')
            add('adGroup','create',{'resourceName':group_rn,'campaign':campaign_rn,'name':name,'type':'SEARCH_STANDARD','status':'ENABLED','cpcBidMicros':str(g['bid'])},f'Create ad group {name} (CPC {g["bid"]/1e6:.2f})')
        else:
            group_rn=old['resourceName']
            if old.get('type')!='SEARCH_STANDARD': raise PlanError(f'{name}: existing group has unexpected type')
            if int(old.get('cpcBidMicros',0))!=g['bid']:
                increase = increase or g['bid'] > int(old.get('cpcBidMicros',0))
                update('adGroup',group_rn,{'cpcBidMicros':str(g['bid'])},f'{name}: CPC {int(old.get("cpcBidMicros",0))/1e6:.2f} -> {g["bid"]/1e6:.2f}')
            if old.get('status')!='ENABLED':
                update('adGroup',group_rn,{'status':'ENABLED'},f'Enable ad group {name}')
        existing = unique([k for k in keywords[group_rn] if not k.get('negative')],lambda k:key(k['keyword']),name+' keyword')
        for k in keywords[group_rn]:
            if k.get('negative'): blockers.append(f'{name}: unmanaged ad group negative {k["keyword"]["text"]}; review manually')
        desired={key(k):k for k in g['keywords']}
        for k, kw in desired.items():
            if k not in existing:
                add('adGroupCriterion','create',{'adGroup':group_rn,'status':'ENABLED','keyword':kw},f'{name}: add [{k[0]}]')
            elif existing[k].get('status')!='ENABLED':
                update('adGroupCriterion',existing[k]['resourceName'],{'status':'ENABLED'},f'{name}: enable [{k[0]}]')
        for k,row in existing.items():
            if k not in desired and row.get('status')!='PAUSED': extra('adGroupCriterion',row,f'{name}: extra keyword {k[1]} {k[0]}')
        # One active RSA per managed group. Replacement creates/reuses a matching ad and pauses superseded RSAs.
        rsa=[a for a in ads[group_rn] if a['ad'].get('type')=='RESPONSIVE_SEARCH_AD']
        other=[a for a in ads[group_rn] if a['ad'].get('type')!='RESPONSIVE_SEARCH_AD' and a.get('status')!='PAUSED']
        for a in other: blockers.append(f'{name}: unmanaged active non-RSA ad {a["resourceName"]}')
        matches=[a for a in rsa if creative(a['ad'])==creative(g['ad'])]
        chosen=next((a for a in matches if a.get('status')=='ENABLED'),matches[0] if matches else None)
        if chosen is None:
            add('adGroupAd','create',{'adGroup':group_rn,'status':'ENABLED','ad':g['ad']},f'{name}: create responsive search ad')
        elif chosen.get('status')!='ENABLED':
            update('adGroupAd',chosen['resourceName'],{'status':'ENABLED'},f'{name}: enable matching responsive search ad')
        for a in rsa:
            if a is not chosen and a.get('status')!='PAUSED':
                pause('adGroupAd',a,f'{name}: pause superseded responsive search ad')
    names={g['name'] for g in config['groups']}
    for name,g in groups.items():
        if name not in names and g.get('status')!='PAUSED':
            if name.startswith('aac:'): extra('adGroup',g,f'Extra ad group {name}')
            else: blockers.append(f'Unmanaged active ad group {name}; review manually')
    # Any existing live campaign mutation needs explicit permission, even if the budget is unchanged.
    live = bool(current and current.get('status')=='ENABLED' and ops)
    return {'version':1,'account':account,'campaign':config['name'],'config_hash':fingerprint(config),'state_hash':fingerprint(state),'prune':prune,'changes':changes,'blockers':blockers,'requires':{'allow_enable':enable,'allow_budget_increase':increase,'allow_live_changes':live},'operations':ops}
