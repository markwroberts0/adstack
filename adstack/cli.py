"""Command-line entry point. No API mutations without a reviewed, fresh plan."""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path
from .config import load, ConfigError
from .api import Client, ApiError, customer_id, snapshot
from .planner import reconcile, PlanError, fingerprint


def write(path,value):
    # Refuse accidental overwrite of a reviewed plan or mutation receipt.
    with Path(path).open('x',encoding='utf-8') as f:
        json.dump(value,f,indent=2,ensure_ascii=False); f.write('\n')

def report(plan):
    print(f"Account: {plan['account']} | Campaign: {plan['campaign']}")
    for c in plan['changes']: print('  ~ '+c)
    if not plan['operations']: print('  No managed changes.')
    for b in plan['blockers']: print('  BLOCKED: '+b)
    for k,v in plan['requires'].items():
        if v: print('  Requires --'+k.replace('_','-'))
    print(f"{len(plan['operations'])} mutation operations; plan SHA256 {fingerprint(plan)}")

def performance(client,days,terms=False):
    if terms:
        q=f"SELECT campaign.name, ad_group.name, search_term_view.search_term, metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions FROM search_term_view WHERE segments.date DURING LAST_{days}_DAYS ORDER BY metrics.cost_micros DESC"
    else:
        q=f"SELECT campaign.name, campaign.status, metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions FROM campaign WHERE segments.date DURING LAST_{days}_DAYS ORDER BY metrics.cost_micros DESC"
    # JSON retains exact micros; Google does not disclose every search term.
    print(json.dumps(client.query(q),indent=2,ensure_ascii=False))

def parser():
    p=argparse.ArgumentParser(prog='ads',description='Declarative Google Search campaign management')
    p.add_argument('--customer-id',default=os.environ.get('ADS_CUSTOMER_ID'),help='10-digit Google Ads customer account ID')
    sub=p.add_subparsers(dest='command',required=True)
    v=sub.add_parser('validate',help='Validate YAML locally; no API calls');v.add_argument('config')
    o=sub.add_parser('preview',help='Offline first-create plan; never contacts Google');o.add_argument('config')
    a=sub.add_parser('plan',help='Read Google Ads and save a reviewable plan');a.add_argument('config');a.add_argument('--out',required=True);a.add_argument('--prune',action='store_true');a.add_argument('--validate-api',action='store_true')
    a=sub.add_parser('apply',help='Re-read state, validate with Google, then optionally commit');a.add_argument('config');a.add_argument('--plan',required=True);a.add_argument('--commit',action='store_true',help='Without this flag, validate only');a.add_argument('--confirm-account');a.add_argument('--receipt');a.add_argument('--allow-enable',action='store_true');a.add_argument('--allow-budget-increase',action='store_true');a.add_argument('--allow-live-changes',action='store_true')
    for cmd in ('performance','search-terms'):
        a=sub.add_parser(cmd);a.add_argument('--days',type=int,choices=[7,14,30],default=7)
    return p

def main(argv=None):
    args=parser().parse_args(argv)
    try:
        if args.command=='validate':
            config=load(args.config);print(f"Valid: {config['name']}; {len(config['groups'])} ad groups; {sum(len(g['keywords']) for g in config['groups'])} exact-match keywords");return 0
        if args.command=='preview':
            config=load(args.config)
            state={'currency':config['currency'],'campaign':None,'budget':None,'groups':[],'keywords':[],'ads':[],'criteria':[]}
            plan=reconcile(config,state,'0000000000');print('OFFLINE PREVIEW — not a valid apply plan');report(plan);return 0
        account=customer_id(args.customer_id)
        if args.command in ('performance','search-terms'):
            performance(Client(account),args.days,args.command=='search-terms');return 0
        config=load(args.config)
        if args.command=='apply':
            saved=json.loads(Path(args.plan).read_text(encoding='utf-8'))
            if saved.get('account')!=account or saved.get('config_hash')!=fingerprint(config): raise PlanError('Plan account or configuration changed; create a new plan')
            if args.commit:
                if args.confirm_account!=account: raise PlanError('Commit requires --confirm-account with the exact 10-digit account ID')
                if not args.receipt: raise PlanError('Commit requires --receipt for a local mutation receipt')
                if Path(args.receipt).exists(): raise PlanError('Receipt path already exists; choose a new path')
        client=Client(account)
        state=snapshot(client,config['name'])
        plan=reconcile(config,state,account,args.prune if args.command=='plan' else saved.get('prune') is True)
        report(plan)
        if args.command=='plan':
            if args.validate_api and plan['operations'] and not plan['blockers']:
                client.mutate(plan['operations'],True);print('Google API validation passed.')
            write(args.out,plan);print('Saved '+args.out)
            return 2 if plan['blockers'] else 0
        if saved!=plan: raise PlanError('Live state or plan changed; re-plan and review before apply')
        if plan['blockers']: raise PlanError('Resolve blockers before apply')
        if not plan['operations']: return 0
        client.mutate(plan['operations'],True);print('Google API validation passed. No changes made yet.')
        if not args.commit:
            print('Validation only. Add --commit and required confirmations to make changes.');return 0
        for flag,required in plan['requires'].items():
            if required and not getattr(args,flag): raise PlanError('Commit requires --'+flag.replace('_','-'))
        result=client.mutate(plan['operations'],False)
        # Never reissue a mutation on receipt-write failure.
        try: write(args.receipt,{'account':account,'plan_sha256':fingerprint(plan),'response':result})
        except OSError as exc: raise ApiError('Google returned success but receipt write failed; re-plan before any further changes') from exc
        print('Applied. Receipt: '+args.receipt);return 0
    except (ConfigError,PlanError,ApiError,OSError,ValueError,KeyError,TypeError) as exc:
        print('Error: '+str(exc),file=sys.stderr);return 1

if __name__=='__main__':
    raise SystemExit(main())
