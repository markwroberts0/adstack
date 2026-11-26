from __future__ import annotations
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
from urllib.parse import urlsplit
import yaml

class ConfigError(ValueError):
    pass

class StrictLoader(yaml.SafeLoader):
    pass

def mapping(loader, node):
    loader.flatten_mapping(node)
    result = {}
    for key, value in node.value:
        k = loader.construct_object(key)
        if not isinstance(k, str) or k in result:
            raise ConfigError(f"Invalid or duplicate YAML key: {k!r}")
        result[k] = loader.construct_object(value)
    return result

StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)

def obj(value, allowed, required, path):
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: expected mapping")
    extra, missing = set(value) - set(allowed), set(required) - set(value)
    if extra or missing:
        raise ConfigError(f"{path}: unknown keys {sorted(extra)}, missing keys {sorted(missing)}")
    return value

def seq(value, path, minimum=0, maximum=1000):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ConfigError(f"{path}: expected {minimum}–{maximum} items")
    return value

def text(value, path, maximum=80):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > maximum or any(ord(c)<32 for c in value):
        raise ConfigError(f"{path}: invalid text (max {maximum} characters)")
    return value

def slug(value, path):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,59}", value):
        raise ConfigError(f"{path}: use lowercase letters, digits and hyphens")
    return value

def money(value, path, maximum):
    try:
        d = Decimal(str(value))
        if not d.is_finite() or not Decimal('0.01') <= d <= Decimal(str(maximum)) or d != d.quantize(Decimal('0.01')):
            raise ValueError()
        return int(d * 1_000_000)
    except (InvalidOperation, ValueError):
        raise ConfigError(f"{path}: expected 0.01–{maximum} with at most 2 decimals") from None

def kw(value, path, exact_only=False):
    value = text(value, path, 82)
    match = 'EXACT' if value.startswith('[') and value.endswith(']') else 'PHRASE' if value.startswith('"') and value.endswith('"') else 'BROAD'
    s = value[1:-1] if match != 'BROAD' else value
    if exact_only and match != 'EXACT':
        raise ConfigError(f"{path}: positive keywords must use [exact match]")
    if not s.strip() or s != s.strip() or len(s)>80 or len(s.split())>10 or any(c in s for c in '[]"'):
        raise ConfigError(f"{path}: invalid keyword {value!r}")
    return {'text': ' '.join(s.lower().split()), 'matchType': match}

def distinct(values, path, key=lambda x:x):
    keys = [key(x) for x in values]
    if len(keys) != len(set(keys)):
        raise ConfigError(f"{path}: duplicate entries")
    return values

def asset(value, path):
    a = obj(value, {'headlines','descriptions','final_url'}, {'headlines','descriptions','final_url'}, path)
    h = distinct([text(x,path+'.headlines',30) for x in seq(a['headlines'],path+'.headlines',3,15)],path+'.headlines',str.casefold)
    d = distinct([text(x,path+'.descriptions',90) for x in seq(a['descriptions'],path+'.descriptions',2,4)],path+'.descriptions',str.casefold)
    u = text(a['final_url'],path+'.final_url',2048)
    p = urlsplit(u)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or any(c.isspace() for c in u):
        raise ConfigError(f"{path}.final_url: expected absolute HTTPS URL")
    return {'finalUrls':[u], 'responsiveSearchAd':{'headlines':[{'text':x} for x in h], 'descriptions':[{'text':x} for x in d]}}

def load(path):
    try:
        raw = yaml.load(Path(path).read_text(encoding='utf-8'), Loader=StrictLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(f'Invalid YAML: {exc}') from exc
    r = obj(raw,{'version','namespace','currency','campaign','ad_templates'},{'version','namespace','currency','campaign','ad_templates'},'config')
    if type(r['version']) is not int or r['version'] != 1: raise ConfigError('version must be 1')
    ns = slug(r['namespace'],'namespace')
    currency = r['currency']
    if not isinstance(currency,str) or not re.fullmatch('[A-Z]{3}',currency): raise ConfigError('currency must be an ISO currency code')
    if not isinstance(r['ad_templates'],dict) or not r['ad_templates']: raise ConfigError('ad_templates must be a nonempty mapping')
    templates = {slug(k,'template name'):asset(v,'ad_templates.'+k) for k,v in r['ad_templates'].items()}
    keys = {'id','status','daily_budget','default_cpc','locations','negative_keywords','ad_groups'}
    c = obj(r['campaign'],keys,keys-{'negative_keywords'},'campaign')
    cid = slug(c['id'],'campaign.id')
    if c['status'] not in ('PAUSED','ENABLED'): raise ConfigError('campaign.status must be PAUSED or ENABLED')
    locations = distinct([str(x) for x in seq(c['locations'],'locations',1,100)],'locations')
    if any(not re.fullmatch(r'[1-9][0-9]{0,11}',x) for x in locations): raise ConfigError('locations must be numeric Google GeoTargetConstant IDs')
    negatives = distinct([kw(x,'negative_keywords') for x in seq(c.get('negative_keywords',[]),'negative_keywords',0,500)],'negative_keywords',lambda x:(x['text'],x['matchType']))
    default = money(c['default_cpc'],'default_cpc',20)
    groups = []
    all_keywords = set()
    for g in seq(c['ad_groups'],'ad_groups',1,100):
        g = obj(g,{'id','keywords','ad_template','cpc'}, {'id','keywords','ad_template'},'ad_group')
        gid = slug(g['id'],'ad_group.id')
        if not isinstance(g['ad_template'],str) or g['ad_template'] not in templates: raise ConfigError(f'{gid}: unknown template')
        keywords = distinct([kw(x,gid+'.keywords',True) for x in seq(g['keywords'],gid+'.keywords',1,500)],gid+'.keywords',lambda x:x['text'])
        for k in keywords:
            if k['text'] in all_keywords: raise ConfigError(f"Keyword assigned to multiple ad groups: {k['text']}")
            all_keywords.add(k['text'])
        groups.append({'name':'aac:'+gid,'bid':money(g['cpc'],gid+'.cpc',20) if 'cpc' in g else default,'keywords':keywords,'ad':templates[g['ad_template']]})
    distinct(groups,'ad_groups',lambda x:x['name'])
    return {'version':1,'name':f'aac:{ns}:{cid}','currency':currency,'status':c['status'],'budget':money(c['daily_budget'],'daily_budget',500),'locations':locations,'negatives':negatives,'groups':groups}
