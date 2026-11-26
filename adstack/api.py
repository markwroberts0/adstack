from __future__ import annotations
import os, re
import requests

VERSION = 'v26'
BASE = f'https://googleads.googleapis.com/{VERSION}'

class ApiError(RuntimeError):
    pass

def customer_id(value):
    value = (value or '').replace('-','')
    if not re.fullmatch(r'[0-9]{10}',value): raise ApiError('Set ADS_CUSTOMER_ID or --customer-id to a 10-digit Google Ads account ID')
    return value

class Client:
    def __init__(self, account):
        self.account = customer_id(account)
        self.http = requests.Session()
        token = os.environ.get('ADS_ACCESS_TOKEN')
        if not token:
            vals = {k:os.environ.get('ADS_'+k.upper()) for k in ('client_id','client_secret','refresh_token')}
            if not all(vals.values()): raise ApiError('Set ADS_ACCESS_TOKEN or ADS_CLIENT_ID, ADS_CLIENT_SECRET, ADS_REFRESH_TOKEN')
            try:
                r = self.http.post('https://oauth2.googleapis.com/token',data={**vals,'grant_type':'refresh_token'},timeout=30)
                r.raise_for_status()
                token = r.json()['access_token']
            except (requests.RequestException,KeyError,ValueError) as exc:
                raise ApiError('OAuth refresh failed; check your local credentials and permissions') from exc
        self.http.headers.update({'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        if os.environ.get('ADS_LOGIN_CUSTOMER_ID'):
            self.http.headers['login-customer-id'] = customer_id(os.environ['ADS_LOGIN_CUSTOMER_ID'])
    def post(self, endpoint, body):
        try:
            r = self.http.post(f'{BASE}/customers/{self.account}/{endpoint}',json=body,timeout=60)
        except requests.RequestException as exc:
            raise ApiError(f'{endpoint}: network failure; mutation outcome may be unknown. Do not retry blindly; re-plan.') from exc
        if not r.ok:
            raise ApiError(f'{endpoint}: HTTP {r.status_code}, request-id={r.headers.get("request-id","unknown")}\n{r.text[:12000]}')
        return r.json()
    def query(self, gaql):
        page = None
        rows = []
        while True:
            body = {'query':gaql}
            if page: body['pageToken'] = page
            data = self.post('googleAds:search',body)
            rows.extend(data.get('results',[]))
            page = data.get('nextPageToken')
            if not page: return rows
    def mutate(self, ops, validate_only):
        return self.post('googleAds:mutate',{'mutateOperations':ops,'partialFailure':False,'validateOnly':validate_only})

def snapshot(client, name):
    # name is constructed exclusively from validated slugs; never interpolate untrusted GAQL.
    if not re.fullmatch(r'aac:[a-z0-9-]+:[a-z0-9-]+',name): raise ApiError('Invalid managed campaign name')
    account = client.query('SELECT customer.id, customer.currency_code FROM customer')
    if len(account)!=1 or str(account[0]['customer']['id'])!=client.account: raise ApiError('Cannot verify target account')
    currency = account[0]['customer']['currencyCode']
    campaigns = client.query(f"SELECT campaign.resource_name, campaign.name, campaign.status, campaign.advertising_channel_type, campaign.campaign_budget, campaign.bidding_strategy_type, campaign.network_settings.target_google_search, campaign.network_settings.target_search_network, campaign.network_settings.target_content_network, campaign.network_settings.target_partner_search_network, campaign.geo_target_type_setting.positive_geo_target_type FROM campaign WHERE campaign.name = '{name}' AND campaign.status != 'REMOVED'")
    if len(campaigns)>1: raise ApiError('Multiple campaigns match the managed name')
    result = {'currency':currency,'campaign':None,'budget':None,'groups':[],'keywords':[],'ads':[],'criteria':[]}
    if not campaigns: return result
    c = campaigns[0]['campaign']; result['campaign'] = c
    rn = c['resourceName']; budget = c['campaignBudget']
    for resource, pattern in [(rn,r'customers/[0-9]+/campaigns/[0-9]+'),(budget,r'customers/[0-9]+/campaignBudgets/[0-9]+')]:
        if not resource.startswith(f'customers/{client.account}/') or not re.fullmatch(pattern,resource): raise ApiError('Unexpected resource name')
    b = client.query(f"SELECT campaign_budget.resource_name, campaign_budget.amount_micros, campaign_budget.explicitly_shared, campaign_budget.reference_count, campaign_budget.period FROM campaign_budget WHERE campaign_budget.resource_name = '{budget}'")
    if len(b)!=1: raise ApiError('Cannot identify campaign budget')
    result['budget'] = b[0]['campaignBudget']
    where = f"campaign.resource_name = '{rn}'"
    queries = {
        'groups': ('adGroup',f'SELECT ad_group.resource_name, ad_group.name, ad_group.status, ad_group.type, ad_group.cpc_bid_micros FROM ad_group WHERE {where} AND ad_group.status != \'REMOVED\''),
        'keywords': ('adGroupCriterion',f'SELECT ad_group_criterion.resource_name, ad_group_criterion.ad_group, ad_group_criterion.status, ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, ad_group_criterion.negative FROM ad_group_criterion WHERE {where} AND ad_group_criterion.type = \'KEYWORD\' AND ad_group_criterion.status != \'REMOVED\' AND ad_group.status != \'REMOVED\''),
        'ads': ('adGroupAd',f'SELECT ad_group_ad.resource_name, ad_group_ad.ad_group, ad_group_ad.status, ad_group_ad.ad.type, ad_group_ad.ad.final_urls, ad_group_ad.ad.responsive_search_ad.headlines, ad_group_ad.ad.responsive_search_ad.descriptions FROM ad_group_ad WHERE {where} AND ad_group_ad.status != \'REMOVED\' AND ad_group.status != \'REMOVED\''),
        'criteria': ('campaignCriterion',f'SELECT campaign_criterion.resource_name, campaign_criterion.status, campaign_criterion.type, campaign_criterion.negative, campaign_criterion.location.geo_target_constant, campaign_criterion.keyword.text, campaign_criterion.keyword.match_type FROM campaign_criterion WHERE {where} AND campaign_criterion.type IN (\'LOCATION\', \'KEYWORD\') AND campaign_criterion.status != \'REMOVED\'')
    }
    for key,(field,q) in queries.items():
        result[key] = sorted((x[field] for x in client.query(q)), key=lambda x:x['resourceName'])
    return result
