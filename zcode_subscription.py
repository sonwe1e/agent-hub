"""Read the currently selected ZCode subscription on this machine.

The on-disk credential format and model rules follow the MIT-licensed
magpie-community ZCode adapter; see docs/ZCODE-LICENSE.txt. Credentials remain
in ZCode's own files and are only held in memory by the local gateway.
"""
from __future__ import annotations

import base64
import copy
import getpass
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
import urllib.parse
import uuid

PROVIDER = 'zcode-subscription'
BASES = {'zai': 'https://api.z.ai/api/anthropic',
         'bigmodel': 'https://open.bigmodel.cn/api/anthropic'}
BUSINESS = {'zai':'https://api.z.ai', 'bigmodel':'https://bigmodel.cn'}
START_BASE = 'https://zcode.z.ai/api/v1/zcode-plan/anthropic'
START_VERSION = '3.16.3'
_DEVICE = str(uuid.uuid4())
_KEY_LOCK = threading.Lock()
_KEY_CACHE = {}


class ZCodeSubscriptionError(ValueError):
    pass


def stream_error(event):
    error=event.get('error') or {}
    typ=error.get('type') if isinstance(error,dict) else ''
    code=str(error.get('code') or event.get('code') or '') if isinstance(error,dict) else ''
    if typ in ('rate_limit_error','quota_error') or code in ('1005','1113','1302','1303'):
        return 'ZCode 订阅额度或速率受限，请查看账号用量后重试'
    if typ in ('authentication_error','permission_error'):
        return 'ZCode 登录或订阅权限不可用，请检查 ZCode 的账号状态'
    return 'ZCode 订阅上游返回失败，请检查订阅和网络'


def stream_status(event):
    error=event.get('error') or {}
    typ=error.get('type') if isinstance(error,dict) else ''
    code=str(error.get('code') or event.get('code') or '') if isinstance(error,dict) else ''
    if typ in ('rate_limit_error','quota_error') or code in ('1005','1113','1302','1303'):return 429
    if typ=='authentication_error':return 401
    if typ=='permission_error':return 403
    return 502


def home():
    from hub import HOME
    return Path(HOME)


def _read(path):
    try:
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError()
        result = json.loads(path.read_text(encoding='utf-8'))
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def data_dir(user_home=None):
    user_home = Path(user_home or home())
    setting = _read(user_home / '.zcode/v2/setting.json')
    moved = setting.get('dataBaseDir')
    base = Path(moved).expanduser() if isinstance(moved, str) and moved.strip() else user_home
    if not base.is_absolute():
        base = user_home
    return base / '.zcode/v2'


def _secret(user_home):
    platform = 'win32' if sys.platform == 'win32' else 'darwin' if sys.platform == 'darwin' else 'linux'
    username = getpass.getuser().split('\\')[-1]
    seed = os.environ.get('ZCODE_CREDENTIAL_SECRET') or f'zcode-credential-fallback:{platform}:{user_home}:{username}'
    return hashlib.sha256(seed.encode()).digest()


def _decrypt(key, value):
    if not isinstance(value, str) or not value.startswith('enc:v1:'):
        return ''
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        values = value[7:].split('.')
        if len(values) != 3:
            return ''
        iv, tag, ciphertext = (base64.urlsafe_b64decode(x + '=' * (-len(x) % 4)) for x in values)
        return AESGCM(key).decrypt(iv, ciphertext + tag, None).decode('utf-8').strip()
    except ImportError:
        raise ZCodeSubscriptionError('缺少 cryptography 依赖，无法读取 ZCode 登录') from None
    except (ValueError, UnicodeError, TypeError):
        return ''
    except Exception:  # invalid AES-GCM tag must not reveal stored content
        return ''


def _expiry(token):
    try:
        part=token.split('.')[1]
        value=json.loads(base64.urlsafe_b64decode(part+'='*(-len(part)%4))).get('exp')
        return value if isinstance(value,(int,float)) and not isinstance(value,bool) else None
    except (ValueError,TypeError,IndexError,AttributeError):
        return None


def _current_identity(auth):
    return tuple(auth.get(k,'') for k in ('family','kind','account','key','token','jwt','org','project','location'))


def binding(auth, local_key):
    """Bind the selected account/plan without writing account IDs or tokens."""
    account=auth.get('account') or auth.get('key') or auth.get('token') or auth.get('jwt') or ''
    identity=json.dumps([auth.get('family'),auth.get('kind'),account,auth.get('org'),auth.get('project')],separators=(',',':'))
    return hmac.new(local_key.encode(),identity.encode(),hashlib.sha256).hexdigest()


def _location(store, key, family, account):
    prefix='project-key-location:v1:'
    matches=[]
    for name,value in store.items():
        if not name.startswith(prefix):continue
        try:
            where=json.loads(urllib.parse.unquote(name[len(prefix):]))
            if not isinstance(where,list) or len(where)<5 or where[1]!=family or where[3] is not None or str(where[4])!='1':continue
            if account and str(where[2])!=account:continue
            location=json.loads(_decrypt(key,value))
            item={k:location.get(k) for k in ('organizationId','projectId','apiKeyId')}
            if all(isinstance(v,str) and v and len(v)<=512 for v in item.values()):matches.append(item)
        except (ValueError,TypeError,IndexError,AttributeError):
            continue
    if len(matches)>1:
        raise ZCodeSubscriptionError('存在多个个人套餐密钥位置，无法确定当前账号；请在 ZCode 重新登录')
    return matches[0] if matches else None


def read_auth(user_home=None):
    user_home = Path(user_home or home())
    root = data_dir(user_home)
    setting = _read(user_home / '.zcode/v2/setting.json')
    store = _read(root / 'credentials.json')
    family = setting.get('providerFamilyDomain')
    if family not in BASES or not store:
        raise ZCodeSubscriptionError('未找到当前 ZCode 登录；请先打开 ZCode 并登录订阅账号')
    selections = setting.get('providerFamilyConnectionSelections') or {}
    selection = selections.get(family) if isinstance(selections, dict) else None
    if not selection:
        # Earlier ZCode versions stored the selected team as an encoded key.
        # Do not accidentally route such a selection through a personal plan.
        keys = setting.get('modelProviderFamilySelectedKeys') or {}
        legacy = keys.get(family) if isinstance(keys, dict) else None
        if isinstance(legacy, str) and legacy:
            prefix = f'team-plan:builtin:{family}-coding-plan:'
            if legacy.startswith(prefix):
                parts = legacy[len(prefix):].split(':')
                if len(parts) != 3:
                    raise ZCodeSubscriptionError('旧版 ZCode 团队选择格式无效，请在 ZCode 重新选择团队')
                selection = {'kind':'team-coding-plan', 'organizationId':urllib.parse.unquote(parts[1]),
                             'projectId':urllib.parse.unquote(parts[2])}
            elif legacy == 'account:zai-start-plan':
                selection = {'kind':'start-plan'}
            elif legacy == f'builtin:{family}-coding-plan' or legacy.startswith(f'account:{family}-individual-coding-plan'):
                selection = {'kind':'individual-coding-plan'}
            else:
                raise ZCodeSubscriptionError('无法识别旧版 ZCode 订阅选择，请在 ZCode 重新选择套餐')
    selection = selection or {}
    if not isinstance(selection, dict):
        raise ZCodeSubscriptionError('ZCode 订阅选择格式无效，请在 ZCode 重新选择套餐')
    kind = selection.get('kind')
    key = _secret(user_home)
    info_raw = _decrypt(key, store.get('oauth:' + family + ':user_info'))
    try:
        info = json.loads(info_raw) if info_raw else {}
    except ValueError:
        info = {}
    account = str(info.get('id') or '') if isinstance(info, dict) else ''
    if kind == 'start-plan':
        jwt=_decrypt(key,store.get('zcodejwttoken')).removeprefix('Bearer ').strip()
        if not jwt:raise ZCodeSubscriptionError('ZCode Start Plan 缺少登录，请在 ZCode 重新登录')
        expiry=_expiry(jwt)
        if expiry is not None and expiry<=time.time():raise ZCodeSubscriptionError('ZCode Start Plan 登录已过期，请在 ZCode 重新登录')
        return {'family':family,'kind':kind,'base':START_BASE,'key':'','jwt':jwt,'account':account}
    if kind == 'team-coding-plan':
        org=selection.get('organizationId');project=selection.get('projectId')
        token=_decrypt(key,store.get('oauth:'+family+':access_token'))
        if not all(isinstance(x,str) and x and len(x)<=512 for x in (org,project)) or not token or len(token)>16384:
            raise ZCodeSubscriptionError('当前团队订阅缺少组织、项目或登录，请先在 ZCode 选择并登录团队')
        jwt=_decrypt(key,store.get('zcodejwttoken')).removeprefix('Bearer ').strip()
        if family=='bigmodel' and jwt and secrets.compare_digest(token,jwt):
            raise ZCodeSubscriptionError('ZCode 团队登录不可用；请在 ZCode 重新登录团队')
        expiry=_expiry(token)
        if expiry is not None and expiry<=time.time():raise ZCodeSubscriptionError('ZCode 团队登录已过期，请在 ZCode 重新登录')
        return {'family':family,'kind':kind,'base':BASES[family],'key':'','token':token,
                'org':org,'project':project,'account':account}
    if kind not in (None,'individual-coding-plan'):
        raise ZCodeSubscriptionError('当前 ZCode 连接类型不是受支持的订阅套餐')
    prefix = f'account-provider:coding-plan:account:{family}-individual-coding-plan:account:'
    candidates = [(name[len(prefix):-len(':api-key')], value) for name, value in store.items()
                  if name.startswith(prefix) and name.endswith(':api-key')]
    other_account=bool(account and candidates and not any(owner == account for owner, _ in candidates))
    if account:
        candidates = [(owner, value) for owner, value in candidates if owner == account]
    if len(candidates) > 1:
        raise ZCodeSubscriptionError('存在多个 ZCode 个人账号，无法确定当前订阅；请在 ZCode 切换并重新登录')
    value = _decrypt(key, candidates[0][1]) if candidates else ''
    if not value and not other_account:
        config = _read(root / 'config.json')
        providers = config.get('provider') if isinstance(config.get('provider'), dict) else {}
        for ident in ('account:' + family + '-individual-coding-plan', 'builtin:' + family + '-coding-plan'):
            candidate = ((providers.get(ident) or {}).get('options') or {}).get('apiKey')
            if isinstance(candidate, str):
                candidate = _decrypt(key, candidate) if candidate.startswith('enc:') else candidate.strip()
                if candidate and '.' in candidate and not re.search(r'[\s{$]', candidate):
                    value = candidate
                    break
    if value and '.' in value and not re.search(r'[\s{$]', value):
        return {'family':family,'kind':'individual-coding-plan','base':BASES[family],'key':value,'account':account}
    location=_location(store,key,family,account)
    token=_decrypt(key,store.get('oauth:'+family+':access_token'))
    if location and token:
        return {'family':family,'kind':'individual-coding-plan','base':BASES[family],'key':'',
                'token':token,'location':location,'account':account}
    raise ZCodeSubscriptionError('当前 ZCode 个人订阅没有可读取的密钥；请在 ZCode 重新登录')


def status(user_home=None):
    try:
        auth = read_auth(user_home)
    except ZCodeSubscriptionError as exc:
        return {'signedIn': False, 'message': str(exc)}
    plan={'individual-coding-plan':'个人 Coding Plan','team-coding-plan':'团队 Coding Plan','start-plan':'Start Plan'}[auth['kind']]
    return {'signedIn':True,'family':auth['family'],'planKind':auth['kind'],
            'message':('智谱' if auth['family']=='bigmodel' else 'Z.ai')+plan+' 已登录'}


def _business_headers(auth):
    token=auth.get('token','')
    value=('Bearer '+token) if auth['family']=='zai' and not token.startswith('Bearer ') else token
    result={'Authorization':value,'User-Agent':'ZCode/'+START_VERSION}
    if auth.get('kind')=='team-coding-plan':
        result.update({'Bigmodel-Organization':auth['org'],'Bigmodel-Project':auth['project'],
                       'Set-Language':'zh' if auth['family']=='bigmodel' else 'en'})
    return result


def _vendor_data(url, headers):
    from agent_hub.quota.usage import fetch_json, UsageError
    try:value=fetch_json(url,headers)
    except UsageError as exc:raise ZCodeSubscriptionError(str(exc)) from None
    if str(value.get('code','200')) not in ('0','200'):
        raise ZCodeSubscriptionError('ZCode 套餐接口拒绝查询，请检查登录和订阅权限')
    return value.get('data')


def resolve_auth(auth):
    """Read an existing key by account/project; never create or persist keys."""
    if auth.get('key') or auth.get('kind')=='start-plan':return dict(auth)
    if not auth.get('token'):
        raise ZCodeSubscriptionError('ZCode 缺少读取套餐密钥所需的登录')
    identity=json.dumps(_current_identity(auth),sort_keys=True,ensure_ascii=False)
    digest=hashlib.sha256(identity.encode()).hexdigest()
    with _KEY_LOCK:
        saved=_KEY_CACHE.get(digest)
        if saved and saved['until']>time.monotonic():return {**auth,'key':saved['key']}
    if auth.get('kind')=='team-coding-plan':
        org,project=auth['org'],auth['project']
    else:
        location=auth.get('location') or {}
        org,project=location.get('organizationId'),location.get('projectId')
    if not all(isinstance(x,str) and x for x in (org,project)):
        raise ZCodeSubscriptionError('ZCode 套餐项目位置无效，请在 ZCode 重新登录')
    path=BUSINESS[auth['family']]+'/api/biz/v1/organization/'+urllib.parse.quote(org,safe='')+'/projects/'+urllib.parse.quote(project,safe='')+'/api_keys'
    headers=_business_headers(auth)
    if auth.get('kind')=='team-coding-plan':
        listing=_vendor_data(path,headers)
        choices=[x.get('apiKey') for x in listing if isinstance(x,dict) and x.get('name')=='zcode-team-api-key'
                 and str(x.get('keyType'))=='2' and isinstance(x.get('apiKey'),str) and x.get('apiKey')] if isinstance(listing,list) else []
        if len(choices)!=1:
            raise ZCodeSubscriptionError('找不到唯一的现有团队套餐密钥；请先在 ZCode 选择并使用该团队')
        ident=choices[0]
    else:
        ident=location.get('apiKeyId')
    if not isinstance(ident,str) or not ident or len(ident)>512:
        raise ZCodeSubscriptionError('ZCode 套餐密钥标识无效')
    copied=_vendor_data(path+'/copy/'+urllib.parse.quote(ident,safe=''),headers)
    secret=copied.get('secretKey') if isinstance(copied,dict) else None
    if isinstance(secret,str) and secret.strip():value=ident+'.'+secret.strip()
    elif auth.get('kind')=='team-coding-plan':value=ident
    else:raise ZCodeSubscriptionError('无法读取现有个人套餐密钥；请在 ZCode 重新登录')
    if re.search(r'[\s{$]',value):raise ZCodeSubscriptionError('ZCode 返回的套餐密钥格式无效')
    with _KEY_LOCK:
        now=time.monotonic()
        for k,v in list(_KEY_CACHE.items()):
            if v['until']<=now:_KEY_CACHE.pop(k,None)
        while len(_KEY_CACHE)>=16:_KEY_CACHE.pop(next(iter(_KEY_CACHE)))
        _KEY_CACHE[digest]={'key':value,'until':now+60}
    return {**auth,'key':value}


def start_balance(auth):
    jwt=auth.get('jwt') or ''
    expiry=_expiry(jwt)
    if not jwt or expiry is not None and expiry<=time.time():
        raise ZCodeSubscriptionError('ZCode Start Plan 登录已过期，请在 ZCode 重新登录')
    url='https://zcode.z.ai/api/v1/zcode-plan/billing/balance?app_version='+START_VERSION
    value=_vendor_data(url,{'Authorization':'Bearer '+jwt,'X-Device-Mid':_DEVICE,'User-Agent':'ZCode/'+START_VERSION})
    if not isinstance(value,dict):raise ZCodeSubscriptionError('ZCode 未返回 Start Plan 额度')
    return value


def _numeric(value):
    if isinstance(value,bool):return None
    try:value=float(value)
    except (TypeError,ValueError):return None
    return value if math.isfinite(value) else None


def _same_plan(bucket, plan):
    bu,pu=bucket.get('user_plan_id'),plan.get('user_plan_id')
    if bu and pu:return bu==pu
    bp,pp=bucket.get('plan_id'),plan.get('plan_id')
    return bp is not None and pp is not None and str(bp)==str(pp)


def _start_models(balance):
    now=_numeric(balance.get('server_time')) or time.time()
    plans=[p for p in balance.get('plans') or [] if isinstance(p,dict)
           and str(p.get('status','')).lower()=='active'
           and (not _numeric(p.get('ends_at')) or _numeric(p['ends_at'])>now)]
    names=[]
    for row in balance.get('balances') or []:
        if not isinstance(row,dict):continue
        matched=any(_same_plan(row,p) for p in plans)
        expires=_numeric(row.get('expires_at'));left=_numeric(row.get('remaining_units'))
        total=_numeric(row.get('total_units'));used=_numeric(row.get('used_units'))
        spent=left is not None and left<=0 or left is None and total is not None and used is not None and used>=total
        if not matched or expires is not None and expires>0 and expires<=now or spent:continue
        capabilities=row.get('capabilities')
        if not isinstance(capabilities,list):continue
        for capability in capabilities:
            if not isinstance(capability,str):continue
            if capability.startswith('model:'):capability=capability[6:]
            elif ':' in capability:continue
            if capability.strip() and len(capability.strip())<=200 and capability.strip().lower() not in {n.lower() for n in names}:names.append(capability.strip())
    return names


def start_request(auth, body, session):
    """Adapt a Start Plan request using ZCode's published client shape.

    Prompt data is from the MIT-licensed magpie-community ZCode adapter;
    the original caller's system instructions and message history are retained.
    """
    import datetime
    import locale
    import platform as platform_module
    prompt=_read(Path(__file__).with_name('zcode_start_prompt.json'))
    if not all(k in prompt for k in ('cliPrefix','stableSections','beforeEnvironment','afterEnvironment','environment','context')):
        raise ZCodeSubscriptionError('缺少 Start Plan 的客户端兼容资料')
    result=copy.deepcopy(body)
    if not isinstance(result.get('messages'),list):raise ZCodeSubscriptionError('Start Plan 请求缺少消息')
    raw=result.get('system');own=[]
    if isinstance(raw,str) and raw.strip():own=[raw]
    elif isinstance(raw,list):
        if any(not isinstance(x,dict) or x.get('type')!='text' for x in raw):
            raise ZCodeSubscriptionError('Start Plan 暂不支持该系统消息格式')
        own=[x['text'] for x in raw if isinstance(x.get('text'),str) and x['text'].strip()]
    elif raw is not None:raise ZCodeSubscriptionError('Start Plan 系统消息格式无效')
    platform='win32' if sys.platform=='win32' else 'darwin' if sys.platform=='darwin' else 'linux'
    os_version=f'{platform} {platform_module.release()} {platform_module.machine()}'
    env=prompt['environment'];provider='bigmodel-api' if auth['family']=='bigmodel' else 'zai-api'
    lines=[env['heading'],env['invokedLine'],f"- {env['platformLabel']}: {platform}",
           f"- {env['osVersionLabel']}: {os_version}",
           env['poweredByLine'].replace('{provider}',provider).replace('{model}',str(result.get('model') or ''))]
    dynamic=[prompt['beforeEnvironment'],'\n'.join(lines),prompt['afterEnvironment'],*own]
    cached=lambda text:{'type':'text','text':text,'cache_control':{'type':'ephemeral'}}
    result['system']=[cached(prompt['cliPrefix']),cached('\n\n'.join(prompt['stableSections'])),cached('\n\n'+'\n\n'.join(dynamic))]
    c=prompt['context'];today=datetime.datetime.now().date().isoformat()
    reminder='\n'.join([c['intro'],c['currentDateHeading']+'\n'+c['currentDateLine'].replace('{date}',today),'',c['outro']])
    result['messages'].insert(0,{'role':'user','content':[{'type':'text','text':'<system-reminder>'+reminder+'</system-reminder>'}]})
    for m in result['messages']:
        if isinstance(m,dict) and isinstance(m.get('content'),list):
            for item in m['content']:
                if isinstance(item,dict):item.pop('cache_control',None)
    last=result['messages'][-1] if result['messages'] else None
    if isinstance(last,dict):
        if isinstance(last.get('content'),str):last['content']=[cached(last['content'])]
        elif isinstance(last.get('content'),list) and last['content'] and isinstance(last['content'][-1],dict):
            last['content'][-1]['cache_control']={'type':'ephemeral'}
    for tool in result.get('tools') or []:
        if isinstance(tool,dict):tool.pop('cache_control',None)
    try:sid=str(uuid.UUID(str(session))) if session else _DEVICE
    except ValueError:sid=str(uuid.uuid5(uuid.NAMESPACE_URL,_DEVICE+'\0'+str(session)))
    meta=result.get('metadata') if isinstance(result.get('metadata'),dict) else {}
    result['metadata']={**meta,'user_id':json.dumps({'device_id':_DEVICE,'account_uuid':'','session_id':sid},separators=(',',':'))}
    language=locale.getlocale()[0] or 'unknown'
    headers={'Authorization':'Bearer '+auth['jwt'],'anthropic-version':'2023-06-01',
             'HTTP-Referer':'https://zcode.z.ai','User-Agent':'ZCode/'+START_VERSION+' ai-sdk/anthropic/3.0.81',
             'X-ZCode-App-Version':START_VERSION,'X-Title':'Z Code@cli','X-Release-Channel':'production',
             'X-Client-Language':language.replace('_','-'),'X-Client-Timezone':os.environ.get('TZ') or 'unknown',
             'X-ZCode-Agent':'glm','X-Platform':platform+'-'+('x64' if platform_module.machine().lower() in ('amd64','x86_64') else platform_module.machine().lower()),
             'X-Os-Category':'windows' if platform=='win32' else 'macos' if platform=='darwin' else 'linux',
             'X-Os-Version':platform_module.release(),'x-request-id':str(uuid.uuid4()),'x-zcode-session-type':'main',
             'x-zcode-trace-id':str(uuid.uuid4()),'x-zcode-query-source':'main_turn','x-session-id':sid}
    return result,headers


def models(auth, user_home=None):
    root = data_dir(user_home) / 'runtime/provider'
    best = None
    for path in root.glob('*/*/*/zcode-builtin.json'):
        candidate = _read(path)
        if candidate and (best is None or (candidate.get('revision') or 0) > (best.get('revision') or 0)):
            best = candidate
    if not best:
        raise ZCodeSubscriptionError('未找到 ZCode 本机模型目录；请先打开并更新 ZCode')
    config = best.get('config') or {}
    plan = 'account:zai-start-plan' if auth.get('kind')=='start-plan' else 'account:' + auth['family'] + '-individual-coding-plan'
    ids = []
    rules = (config.get('providerConfigRules') or {}).get('providerRules') or []
    for rule in rules:
        if rule.get('providerId') == plan:
            ids.extend(rule.get('builtinModelIds') or [])
    model_rules = config.get('modelConfigRules') or {}
    disabled = set()
    for rule in model_rules.get('builtinProviderModelRules') or []:
        if rule.get('providerId') != plan or not isinstance(rule.get('modelId'), str):
            continue
        if (rule.get('config') or {}).get('enabled') is False:
            disabled.add(rule['modelId'].lower())
        else:
            ids.append(rule['modelId'])
    result, seen = [], set()
    for ident in ids:
        if not isinstance(ident, str) or not ident or ident.lower() in seen or ident.lower() in disabled:
            continue
        seen.add(ident.lower())
        model = {'id': ident, 'name': ident}
        for rule in model_rules.get('modelRules') or []:
            try:
                match = re.fullmatch(rule.get('modelMatch', '(?!)'), ident, re.I)
            except (re.error, TypeError):
                continue
            if not match:
                continue
            cfg = rule.get('config') or {}; props = cfg.get('properties') or {}; opts = cfg.get('optionSpecs') or {}
            window = props.get('contextWindow'); maximum = (opts.get('maxOutputTokens') or {}).get('max')
            if type(window) is int and window > 0: model['contextWindow'] = window
            if type(maximum) is int and maximum > 0: model['maxTokens'] = maximum
            inputs = props.get('inputFormat') or {}
            if isinstance(inputs.get('supportsImage'), bool):
                model['input'] = ['text', 'image'] if inputs['supportsImage'] else ['text']
            levels = (opts.get('reasoningLevel') or {}).get('values')
            if isinstance(levels, list) and levels: model['reasoning'] = True
        result.append(model)
    if auth.get('kind')=='start-plan':
        allowed=_start_models(start_balance(auth))
        if not allowed:raise ZCodeSubscriptionError('ZCode Start Plan 暂无可用模型或额度；请检查 ZCode 的套餐状态')
        catalog={m['id'].lower():m for m in result}
        result=[catalog.get(name.lower(),{'id':name,'name':name}) for name in allowed]
    if not result:
        raise ZCodeSubscriptionError('ZCode 本机目录没有当前订阅的模型；请先更新 ZCode')
    return result


def provider_config(cfg, entries, existing=None, auth=None):
    from gateway_runtime import gateway_url
    p = copy.deepcopy(existing or {})
    existing_agents = p.get('agents') if isinstance(p.get('agents'), dict) else {}
    targets = p.get('targets') if isinstance(p.get('targets'), list) else ['claude', 'codex', 'pi', 'dsh', 'opencode']
    p.update(name='ZCode', displayName='ZCode 订阅', kind=PROVIDER,
             baseUrl=gateway_url(cfg)+'/zcode-subscription/pi/v1', api='openai-completions',
             apiKey=p.get('apiKey') or secrets.token_urlsafe(32), enabled=True, targets=targets, models=entries)
    p['agents'] = {**existing_agents, **{agent: {**(existing_agents.get(agent) if isinstance(existing_agents.get(agent), dict) else {}),
        'baseUrl': gateway_url(cfg)+'/zcode-subscription/'+agent+('' if agent=='claude' else '/v1'), 'api': api}
        for agent, api in [('claude','anthropic-messages'), ('codex','openai-responses'),
                           ('pi','openai-completions'), ('dsh','openai-completions'), ('opencode','openai-completions')]}}
    p['notes'] = '使用本机 ZCode 当前选中的订阅，不创建密钥；请保持 Hub 运行。'
    if auth:
        p['subscriptionBinding']=binding(auth,p['apiKey'])
        p['subscriptionModels']=[x['id'] for x in entries]
        p['subscriptionFamily']=auth['family']
        p['subscriptionPlanKind']=auth['kind']
    return p


def _parts(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [{'type': 'text', 'text': value}] if value else []
    if not isinstance(value, list):
        raise ValueError('ZCode 不支持此消息内容格式')
    result = []
    for part in value:
        typ = part.get('type') if isinstance(part, dict) else None
        if typ == 'text':
            result.append({'type': 'text', 'text': part.get('text', '')})
        elif typ == 'image_url':
            url = part.get('image_url'); url = url.get('url') if isinstance(url, dict) else url
            if not isinstance(url, str): raise ValueError('图片地址无效')
            if url.startswith('data:'):
                match = re.fullmatch(r'data:(image/[a-zA-Z0-9.+-]+);base64,(.+)', url, re.S)
                if not match: raise ValueError('图片格式无效')
                source = {'type':'base64', 'media_type':match.group(1), 'data':match.group(2)}
            elif url.startswith(('https://', 'http://')):
                source = {'type':'url', 'url':url}
            else: raise ValueError('不支持的图片地址')
            result.append({'type':'image', 'source':source})
        else: raise ValueError('ZCode 不支持此消息内容类型：'+str(typ))
    return result


def request_body(body, protocol, model):
    from agent_hub.gateway.protocol import ToolNames, to_chat
    if body.get('background') or body.get('previous_response_id'):
        raise ValueError('ZCode 需要完整消息历史，暂不支持后台或 previous_response_id')
    if protocol == 'responses' and any(isinstance(t,dict) and t.get('type') in ('web_search','web_search_preview') for t in body.get('tools') or []):
        raise ValueError('ZCode 暂不支持服务端网页搜索；请在 Codex 设置 web_search = "disabled" 后重试')
    if protocol == 'anthropic':
        request = copy.deepcopy(body)
        request.update(model=model, stream=True)
        return request, ToolNames()
    chat, names = to_chat(dict(body, model=model), protocol, normalize_client_metadata=False,
                          preserve_tool_parts=True, preserve_reasoning_summary=True)
    instructions, messages = [], []
    def add(role, parts):
        if not parts: return
        if messages and messages[-1]['role'] == role:
            messages[-1]['content'].extend(parts)
        else:
            messages.append({'role':role, 'content':parts})
    for message in chat['messages']:
        role = message.get('role')
        if role == 'system':
            parts = _parts(message.get('content'))
            if any(p['type'] != 'text' for p in parts): raise ValueError('系统指令不支持图片')
            instructions.extend(p['text'] for p in parts)
        elif role == 'tool':
            value = message.get('content')
            if isinstance(value,list):value=_parts(value)
            elif not isinstance(value,str):raise ValueError('ZCode 的工具结果须为文本或图片')
            add('user', [{'type':'tool_result','tool_use_id':message.get('tool_call_id',''),'content':value}])
        elif role in ('user', 'assistant'):
            parts = _parts(message.get('content'))
            for call in message.get('tool_calls') or []:
                fn = call.get('function') or {}
                try: arguments = json.loads(fn.get('arguments') or '{}')
                except (ValueError, TypeError): raise ValueError('工具调用参数必须是有效 JSON') from None
                if not isinstance(arguments, dict): raise ValueError('工具调用参数必须是 JSON 对象')
                parts.append({'type':'tool_use','id':call.get('id',''),'name':fn.get('name',''),'input':arguments})
            add(role, parts)
        else: raise ValueError('ZCode 不支持消息角色：'+str(role))
    if not messages: raise ValueError('请提供至少一条消息')
    request = {'model':model, 'messages':messages, 'stream':True,
               'max_tokens':chat.get('max_tokens') or 8192}
    if instructions: request['system'] = '\n\n'.join(instructions)
    for name in ('temperature','top_p'):
        if name in chat: request[name] = chat[name]
    if 'stop' in chat:
        stop = chat['stop']; request['stop_sequences'] = [stop] if isinstance(stop, str) else stop
    if chat.get('tools'):
        request['tools'] = [{'name':t['function']['name'], 'description':t['function'].get('description',''),
                             'input_schema':t['function'].get('parameters',{})} for t in chat['tools']]
    choice = chat.get('tool_choice')
    if choice:
        request['tool_choice'] = {'type': 'any' if choice == 'required' else 'auto' if choice == 'auto' else 'tool',
                                  **({'name':choice} if choice not in ('required','auto') else {})}
    return request, names


class AnthropicToChat:
    """Convert Anthropic events to Chat deltas for Reply's three output formats."""
    def __init__(self):
        self.finished = False
        self.reason = None
        self.usage = {}
        self.blocks = {}

    def feed(self, event):
        typ = event.get('type'); index = event.get('index', 0)
        if typ == 'message_start':
            usage = (event.get('message') or {}).get('usage') or {}
            total = usage.get('input_tokens', 0) + usage.get('cache_read_input_tokens', 0) + usage.get('cache_creation_input_tokens', 0)
            self.usage = {'prompt_tokens':total, 'prompt_tokens_details':{'cached_tokens':usage.get('cache_read_input_tokens',0)}}
        elif typ == 'content_block_start':
            block = event.get('content_block') or {}; kind = block.get('type'); self.blocks[index] = kind
            if kind == 'tool_use':
                value = block.get('input') or {}; args = json.dumps(value,ensure_ascii=False) if value else ''
                return [{'choices':[{'index':0,'delta':{'tool_calls':[{'index':index,'id':block.get('id',''),
                         'type':'function','function':{'name':block.get('name',''),'arguments':args}}]}}]}]
            if kind == 'text' and block.get('text'):
                return [{'choices':[{'index':0,'delta':{'content':block['text']}}]}]
            if kind == 'thinking' and block.get('thinking'):
                return [{'choices':[{'index':0,'delta':{'reasoning_content':block['thinking']}}]}]
            if kind not in ('text','thinking','redacted_thinking'):
                raise ValueError('ZCode 返回未知内容类型：'+str(kind))
        elif typ == 'content_block_delta':
            delta = event.get('delta') or {}; kind = delta.get('type')
            if kind == 'text_delta': return [{'choices':[{'index':0,'delta':{'content':delta.get('text','')}}]}]
            if kind == 'thinking_delta': return [{'choices':[{'index':0,'delta':{'reasoning_content':delta.get('thinking','')}}]}]
            if kind == 'input_json_delta':
                if self.blocks.get(index) != 'tool_use': raise ValueError('ZCode 工具参数缺少工具身份')
                return [{'choices':[{'index':0,'delta':{'tool_calls':[{'index':index,'function':{'arguments':delta.get('partial_json','')}}]}}]}]
            if kind != 'signature_delta': raise ValueError('ZCode 返回未知流片段：'+str(kind))
        elif typ == 'message_delta':
            reason = (event.get('delta') or {}).get('stop_reason')
            self.reason = {'end_turn':'stop','stop_sequence':'stop','tool_use':'tool_calls','max_tokens':'length'}.get(reason)
            if not self.reason: raise ValueError('ZCode 返回未知停止原因')
            usage = event.get('usage') or {}
            if 'output_tokens' in usage: self.usage['completion_tokens'] = usage['output_tokens']
            self.usage['total_tokens'] = self.usage.get('prompt_tokens',0)+self.usage.get('completion_tokens',0)
            return [{'choices':[{'index':0,'delta':{},'finish_reason':self.reason}], 'usage':self.usage}]
        elif typ == 'message_stop':
            if not self.reason: raise ValueError('ZCode 响应缺少停止原因')
            self.finished = True
        elif typ == 'error': raise ValueError(stream_error(event))
        return []


class AnthropicCollector:
    """Assemble a non-streamed native response, retaining thinking signatures."""
    def __init__(self, model):
        self.value = None; self.parts = {}; self.arguments = {}; self.model = model; self.finished = False
    def feed(self, event):
        typ = event.get('type'); index = event.get('index', 0)
        if typ == 'message_start':
            self.value = copy.deepcopy(event.get('message') or {})
            self.value['model'] = self.model; self.value['content'] = []
        elif typ == 'content_block_start':
            self.parts[index] = copy.deepcopy(event.get('content_block') or {})
            if self.parts[index].get('type') == 'tool_use': self.arguments[index] = ''
        elif typ == 'content_block_delta':
            if index not in self.parts: raise ValueError('ZCode 流缺少内容起点')
            part = self.parts[index]; delta = event.get('delta') or {}; kind = delta.get('type')
            if kind == 'text_delta': part['text'] = part.get('text','') + delta.get('text','')
            elif kind == 'thinking_delta': part['thinking'] = part.get('thinking','') + delta.get('thinking','')
            elif kind == 'signature_delta': part['signature'] = part.get('signature','') + delta.get('signature','')
            elif kind == 'input_json_delta': self.arguments[index] = self.arguments.get(index,'') + delta.get('partial_json','')
            else: raise ValueError('ZCode 返回未知流片段')
        elif typ == 'content_block_stop' and index in self.arguments:
            try: self.parts[index]['input'] = json.loads(self.arguments[index] or '{}')
            except ValueError: raise ValueError('ZCode 返回无效工具参数') from None
        elif typ == 'message_delta':
            if self.value is None: raise ValueError('ZCode 流缺少消息起点')
            self.value.update(event.get('delta') or {})
            self.value['usage'] = {**(self.value.get('usage') or {}), **(event.get('usage') or {})}
        elif typ == 'message_stop':
            if self.value is None or not self.value.get('stop_reason'): raise ValueError('ZCode 流缺少完整消息')
            self.value['content'] = [self.parts[i] for i in sorted(self.parts)]; self.finished = True
        elif typ == 'error': raise ValueError(stream_error(event))
