"""Read-only subscription quota snapshots; responses are strictly allowlisted."""
from __future__ import annotations

import http.client
import json
import math
import time

from agent_hub.gateway.transport import ProxyError, connection_for


class UsageError(ValueError):
    pass


def fetch_json(url, headers, *, timeout=20):
    connection = None
    try:
        connection, target, proxy_headers = connection_for(url, timeout=timeout)
        connection.request('GET', target, headers={'Accept':'application/json','Accept-Encoding':'identity',**headers,**proxy_headers})
        response = connection.getresponse()
        data = response.read(1024*1024+1)
        if response.status != 200:
            raise UsageError(f'额度查询失败（HTTP {response.status}），请检查登录、套餐权限和网络')
        if len(data) > 1024*1024:
            raise UsageError('额度响应超过大小限制')
        value = json.loads(data)
        if not isinstance(value,dict):
            raise UsageError('额度响应格式无效')
        return value
    except ProxyError as exc:
        raise UsageError(str(exc)) from None
    except UsageError:
        raise
    except (OSError,http.client.HTTPException,ValueError,TypeError):
        raise UsageError('额度查询不可用，请检查网络并重试') from None
    finally:
        if connection is not None:connection.close()


def _num(value):
    if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value):
        return value
    return None


def codex_quota(auth, version, *, endpoint='https://chatgpt.com/backend-api/wham/usage'):
    from codex_subscription import headers
    data=fetch_json(endpoint,headers(auth,version))
    rate=data.get('rate_limit')
    if not isinstance(rate,dict):raise UsageError('Codex 未返回可识别的额度窗口')
    windows=[]
    for name,ident in [('主窗口','primary_window'),('次窗口','secondary_window')]:
        raw=rate.get(ident)
        if not isinstance(raw,dict):continue
        used=_num(raw.get('used_percent'));seconds=_num(raw.get('limit_window_seconds'));reset=_num(raw.get('reset_at'))
        windows.append({'name':name,'usedPercent':used,'windowSeconds':seconds,'resetsAt':reset})
    if not windows:raise UsageError('Codex 未返回可识别的额度窗口')
    return {'source':'Codex 账号用量接口','checkedAt':time.time(),
            'plan':data.get('plan_type') if isinstance(data.get('plan_type'),str) else None,'windows':windows}


def zcode_quota(auth, *, endpoint=None):
    import urllib.parse
    if auth.get('kind')=='start-plan':
        from zcode_subscription import start_balance, _start_models, _same_plan, _numeric
        data=start_balance(auth);windows=[]
        now=_numeric(data.get('server_time')) or time.time()
        plans=[x for x in data.get('plans') or [] if isinstance(x,dict) and str(x.get('status','')).lower()=='active'
               and (not _numeric(x.get('ends_at')) or _numeric(x.get('ends_at'))>now)]
        for item in data.get('balances') or []:
            if not isinstance(item,dict):continue
            match=any(_same_plan(item,p) for p in plans)
            expires=_numeric(item.get('expires_at'))
            if not match or expires is not None and expires>0 and expires<=now:continue
            total=_numeric(item.get('total_units'));left=_numeric(item.get('remaining_units'));used=_numeric(item.get('used_units'))
            if total is not None and left is not None:used=total-left
            percent=used*100/total if total is not None and total>0 and used is not None else None
            windows.append({'name':item.get('show_name') if isinstance(item.get('show_name'),str) else 'Start Plan 额度',
                            'usedPercent':percent,'used':used,'total':total,'expiresAt':expires})
        return {'source':'ZCode Start Plan 用量接口','checkedAt':time.time(),'plan':'Start Plan',
                'windows':windows,'availableModels':_start_models(data)}
    if endpoint:
        base=endpoint
    elif auth.get('kind')=='team-coding-plan':
        from zcode_subscription import BUSINESS
        base=BUSINESS[auth['family']]+'/api/monitor/usage/quota/limit'
    else:
        base=urllib.parse.urlsplit(auth['base'])._replace(path='',query='',fragment='').geturl()+'/api/monitor/usage/quota/limit'
    if auth.get('kind')=='team-coding-plan':
        base += ('&' if '?' in base else '?')+'type=2'
    headers={'Authorization':auth['key']}
    if auth.get('kind')=='team-coding-plan':
        headers.update({'Bigmodel-Organization':auth['org'],'Bigmodel-Project':auth['project'],
                        'Set-Language':'zh' if auth['family']=='bigmodel' else 'en'})
    envelope=fetch_json(base,headers)
    if str(envelope.get('code','200')) not in ('0','200') or not isinstance(envelope.get('data'),dict):
        raise UsageError('ZCode 未返回可识别的套餐额度，请稍后重试')
    data=envelope['data'];windows=[]
    for row in data.get('limits') or []:
        if not isinstance(row,dict):continue
        unit=row.get('unit'); count=_num(row.get('number')) or 1
        span={1:60,3:3600,4:86400,5:30*86400,6:7*86400}.get(unit)
        if unit==3 and row.get('number') in (None,0):count=5
        seconds=span*count if span else None
        typ=str(row.get('type') or '')
        if typ.upper()=='TIME_LIMIT':name='MCP 工具调用'
        elif seconds==5*3600:name='5 小时窗口'
        elif seconds==7*86400:name='每周窗口'
        elif seconds:name=f'{int(seconds//3600)} 小时窗口'
        else:name='套餐窗口'
        used=_num(row.get('percentage'));total=_num(row.get('usage'));left=_num(row.get('remaining'))
        current=_num(row.get('currentValue'))
        if total is not None and total>0:
            if left is not None:current=total-left
            if current is not None:used=current*100/total
        elif total==0:
            # Zero is documented by the vendor adapter as an uncapped window,
            # not a 0% or 100% plan usage. Preserve the lack of a number.
            used=None;total=None;current=None
        reset=_num(row.get('nextResetTime'))
        if reset and reset>100000000000:reset=reset/1000
        windows.append({'name':name,'usedPercent':used,'windowSeconds':seconds,'resetsAt':reset,
                        'used':current,'total':total})
    if not windows:raise UsageError('ZCode 未返回可识别的额度窗口')
    return {'source':'ZCode Coding Plan 用量接口','checkedAt':time.time(),
            'plan':data.get('level') if isinstance(data.get('level'),str) else None,'windows':windows}
