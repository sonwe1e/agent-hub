"""WorkBuddy AI browser authentication and catalog, owned by Agent Hub.

Protocol reference: magpie-community/plugins packages/workbuddy (MIT).
OAuth credentials stay in the ignored gateway data directory, never providers.json.
See docs/WORKBUDDY-LICENSE.txt for the protocol reference's license.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = 'https://www.workbuddy.ai'
UA = 'WorkBuddy/5.5.6'
PROVIDER = 'workbuddy-ai'
_LOCK = threading.RLock()


class WorkBuddyError(ValueError):
    def __init__(self, message, code=0, status=502):
        super().__init__(message)
        self.code, self.status = code, status


def call(path, method='GET', headers=None, body=None):
    request = urllib.request.Request(ENDPOINT + path,
        data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={'Content-Type':'application/json', 'Accept':'application/json',
                 'User-Agent':UA, **(headers or {})})
    try:
        response = urllib.request.urlopen(request, timeout=25)
    except urllib.error.HTTPError as exc:
        response = exc
    except OSError:
        raise WorkBuddyError('无法连接 WorkBuddy AI，请检查网络后重试') from None
    with response:
        status = response.status
        try:
            data = json.loads(response.read(8 * 1024 * 1024))
        except (ValueError, OSError):
            raise WorkBuddyError('WorkBuddy AI 返回无效数据', status=status) from None
    if not isinstance(data,dict): raise WorkBuddyError('WorkBuddy AI 返回无效数据')
    try:code=int(data.get('code') or 0)
    except (TypeError,ValueError):raise WorkBuddyError('WorkBuddy AI 返回无效错误码') from None
    if status >= 400 or code != 0:
        # Do not echo vendor text: it can contain OAuth tokens or login state.
        raise WorkBuddyError(f'WorkBuddy AI 请求失败（HTTP {status}，code {code}）', code, status)
    value=data.get('data') or {}
    if not isinstance(value,dict): raise WorkBuddyError('WorkBuddy AI 返回无效数据')
    return value


def merge_token(auth, token):
    if not isinstance(token.get('accessToken'), str) or not token['accessToken']:
        raise WorkBuddyError('WorkBuddy AI 未返回有效登录凭据')
    result = {**auth, 'access':token['accessToken']}
    for source, target in [('refreshToken','refresh'), ('domain','domain'), ('tokenType','tokenType')]:
        if token.get(source): result[target] = token[source]
    for key, absolute, relative in [('expires','expiresAt','expiresIn'),
                                    ('refreshExpiresAt','refreshExpiresAt','refreshExpiresIn')]:
        if token.get(absolute, 0) > 0: result[key] = token[absolute] / 1000
        elif token.get(relative, 0) > 0: result[key] = time.time() + token[relative]
    return result


def headers_for(auth):
    headers = {'Authorization':'Bearer '+auth['access'], 'X-User-Id':str(auth.get('uid') or ''),
        'X-Domain':auth.get('domain') or 'www.workbuddy.ai', 'X-Product':'SaaS',
        'X-IDE-Type':'WorkBuddy', 'User-Agent':UA, 'X-Requested-With':'XMLHttpRequest',
        'X-Agent-Intent':'craft', 'X-Agent-Purpose':'conversation', 'X-IDE-Name':'WorkBuddy',
        'X-IDE-Version':'5.5.6', 'X-Agent-Type':'main', 'Content-Type':'application/json',
        'Accept':'text/event-stream', 'Accept-Encoding':'identity'}
    if auth.get('enterpriseId'):
        headers.update({'X-Enterprise-Id':auth['enterpriseId'], 'X-Tenant-Id':auth['enterpriseId']})
    return headers


class Accounts:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'workbuddy-auth.json'

    @contextmanager
    def locked(self):
        # Hub and its gateway are different processes. A shared file lock keeps
        # a rotating refresh token from being spent twice by concurrent requests.
        self.directory.mkdir(parents=True, exist_ok=True)
        with _LOCK, (self.directory / 'workbuddy-auth.lock').open('a+b') as lock:
            lock.seek(0, 2)
            if not lock.tell(): lock.write(b'0'); lock.flush()
            lock.seek(0)
            if os.name == 'nt':
                import msvcrt
                deadline = time.monotonic() + 60
                while True:
                    try: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1); break
                    except OSError:
                        if time.monotonic() > deadline: raise WorkBuddyError('登录凭据正忙，请稍后重试')
                        time.sleep(.05)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX)
            try: yield
            finally:
                lock.seek(0)
                if os.name == 'nt': msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else: fcntl.flock(lock, fcntl.LOCK_UN)

    def read(self):
        try: return json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError: return {}
        except (ValueError, OSError): raise WorkBuddyError('WorkBuddy AI 登录文件无法读取，请重新登录') from None

    def save(self, auth):
        from gateway_runtime import write_json
        write_json(self.path, auth)

    def fresh(self):
        with self.locked():
            auth = self.read()
            if not auth.get('access'): raise WorkBuddyError('请先在设置中登录 WorkBuddy AI', status=401)
            if not auth.get('expires') or auth['expires'] > time.time()+60: return auth
            if auth.get('refresh') and auth.get('refreshExpiresAt', float('inf')) > time.time():
                try:
                    token = call('/v2/plugin/auth/token/refresh', 'POST', {
                        'X-Refresh-Token':auth['refresh'], 'X-Auth-Refresh-Source':'plugin',
                        'X-Domain':auth.get('domain') or 'www.workbuddy.ai',
                        **{k:v for k,v in headers_for(auth).items() if k in ('X-Enterprise-Id','X-Tenant-Id')}}, {})
                    auth = merge_token(auth, token)
                    self.save(auth)
                except WorkBuddyError:
                    if auth.get('expires',0) <= time.time():
                        raise WorkBuddyError('WorkBuddy AI 登录已过期且刷新失败，请重新登录', status=401) from None
            elif auth.get('expires',0) <= time.time():
                raise WorkBuddyError('WorkBuddy AI 登录已过期，请重新登录', status=401)
            return auth

    def models(self):
        auth = self.fresh()
        cfg = call('/v3/config', headers={**headers_for(auth), 'User-Agent':'CLI/2.0.0 '+UA,
                                        'Accept':'application/json'})
        details = {m['id']:m for m in cfg.get('models',[]) if isinstance(m,dict) and m.get('id')}
        rows = {}
        for agent in cfg.get('agents',[]):
            if agent.get('name') != 'cli': continue
            for mid in agent.get('models',[]):
                if not isinstance(mid,str) or not mid: continue
                d = details.get(mid,{})
                m = {'id':mid, 'name':d.get('name') or mid,
                     'input':['text','image'] if d.get('supportsImages') is True and not mid.startswith('glm-') else ['text'],
                     'reasoning':bool((d.get('reasoning') or {}).get('supportedEfforts'))}
                for source,target in [('maxInputTokens','contextWindow'),('maxOutputTokens','maxTokens')]:
                    if isinstance(d.get(source),(int,float)) and d[source] > 0: m[target] = d[source]
                # Credit multipliers are not USD token prices.
                rows[mid] = m
        if not rows: raise WorkBuddyError('WorkBuddy AI 未返回此账号可用的 CLI 模型；请稍后刷新')
        return list(rows.values())

    def status(self):
        with self.locked(): auth = self.read()
        return {'signedIn':bool(auth.get('access')), 'account':auth.get('account',''),
                'enterprise':bool(auth.get('enterpriseId')), 'expiresAt':auth.get('expires'),
                'expired':bool(auth.get('expires') and auth['expires'] <= time.time())}


class Login:
    def __init__(self, accounts):
        self.accounts = accounts
        self.lock = threading.RLock()
        self.pending = None

    def start(self):
        data = call('/v2/plugin/auth/state?platform=workbuddy-ai', 'POST',
                    {k:'true' for k in ('X-No-Authorization','X-No-User-Id','X-No-Enterprise-Id','X-No-Department-Info')}, {})
        url = urllib.parse.urlsplit(data.get('authUrl',''))
        if not data.get('state') or url.scheme != 'https' or not url.hostname or url.username or url.password:
            raise WorkBuddyError('WorkBuddy AI 未返回有效登录页面')
        query = dict(urllib.parse.parse_qsl(url.query))
        query.update(version='2.0.0', loginSessionId=secrets.token_hex(16))
        link = urllib.parse.urlunsplit(url._replace(query=urllib.parse.urlencode(query)))
        with self.lock:
            self.cancel()
            pending = {'id':secrets.token_urlsafe(24), 'status':'pending', 'url':link,
                       'expiresAt':time.time()+300, 'cancel':threading.Event(), 'error':''}
            self.pending = pending
        threading.Thread(target=self._poll,args=(pending,data['state']),daemon=True).start()
        return self.status()

    def cancel(self):
        with self.lock:
            if self.pending:
                self.pending['cancel'].set()
                self.pending = None

    def status(self):
        with self.lock:
            if not self.pending: return None
            return {k:v for k,v in self.pending.items() if k != 'cancel'}

    def _poll_call(self, pending, path, headers, codes):
        while not pending['cancel'].is_set() and time.time() < pending['expiresAt']:
            try:
                value = call(path, headers=headers)
                if value: return value
            except WorkBuddyError as exc:
                if exc.code not in codes and exc.status not in (408,429): raise
            pending['cancel'].wait(1)
        raise WorkBuddyError('登录已取消或超时，请重新登录')

    def _poll(self, pending, state):
        try:
            q = urllib.parse.quote(str(state),safe='')
            token = self._poll_call(pending,'/v2/plugin/auth/token?state='+q,{},(11217,))
            auth = merge_token({}, token)
            who = self._poll_call(pending,'/v2/plugin/login/account?state='+q,
                {'Authorization':'Bearer '+auth['access'], 'X-Domain':auth.get('domain') or 'www.workbuddy.ai',
                 'X-No-User-Id':'true','X-No-Enterprise-Id':'true'},(12151,))
            auth.update(uid=str(who.get('uid') or ''), account=who.get('nickname') or who.get('uid') or 'WorkBuddy AI')
            enterprise = who.get('enterpriseId') or ''
            if not enterprise and str(who.get('type') or 'personal').lower() != 'personal':
                listing = call('/v2/plugin/accounts',headers=headers_for(auth))
                matches = [a for a in listing.get('accounts',[]) if a.get('uid') == who.get('uid') and a.get('enterpriseId')]
                same = [a for a in matches if a.get('type') == who.get('type')]
                last = [a for a in matches if a.get('lastLogin')]
                picks = same if len(same)==1 else last if len(last)==1 else matches
                if len(picks)!=1: raise WorkBuddyError('无法确认企业账号身份，请重新选择账号登录')
                enterprise = picks[0]['enterpriseId']
            if enterprise: auth['enterpriseId'] = str(enterprise)
            with self.lock:
                if self.pending is not pending or pending['cancel'].is_set(): return
                with self.accounts.locked(): self.accounts.save(auth)
                pending.update(status='succeeded',url='')
        except (ValueError, OSError, TypeError, KeyError):
            with self.lock:
                if self.pending is pending:
                    pending.update(status='failed',url='',error='WorkBuddy AI 登录未完成或已超时，请重新登录')


def provider_config(cfg, models, existing=None):
    from gateway_runtime import gateway_url
    p = copy.deepcopy(existing or {})
    p.update(name='WorkBuddy AI', displayName='WorkBuddy AI（海外版）', kind=PROVIDER,
             baseUrl=gateway_url(cfg)+'/workbuddy/pi/v1', api='openai-completions',
             apiKey=p.get('apiKey') or secrets.token_urlsafe(32), enabled=True,
             targets=['claude','codex','pi','dsh'], models=models)
    p['agents'] = {a:{'baseUrl':gateway_url(cfg)+'/workbuddy/'+a+('' if a=='claude' else '/v1'),
                       'api':api} for a,api in [('claude','anthropic-messages'),('codex','openai-responses'),
                                                ('pi','openai-completions'),('dsh','openai-completions')]}
    p['notes'] = 'WorkBuddy AI 海外版订阅，经 Hub 本地网关转换。登录凭据只保存在 data/gateway 中；使用时请保持 Hub 运行。'
    return p
