"""Agent Hub's own loopback gateway. No external engine or account discovery.

Wire bytes are forwarded as they arrive. Observation is a bounded side reader;
ordinary connections keep their wire protocol. Supported subscriptions have explicit adapters.
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import ssl
import threading
import time
import urllib.parse
import uuid
from agent_hub.gateway.transport import ProxyError, connection_for

MAX_BODY = 32 * 1024 * 1024
MAX_DECODED = 128 * 1024 * 1024
MAX_CAPTURE = 8 * 1024 * 1024
BODY_BYTES = 256 * 1024 * 1024
ROUTE_BYTES = 200 * 1024 * 1024
HOP = {'connection','keep-alive','proxy-authenticate','proxy-authorization',
       'te','trailer','transfer-encoding','upgrade','host','content-length'}


def packed(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()


def subscription_failure(name, status):
    advice={400:'请检查模型、消息和工具参数',401:'登录已失效，请在原应用重新登录',
            403:'账号无权使用该模型或套餐',404:'上游模型或接口不存在，请刷新模型目录',
            408:'请求超时，请检查网络后重试',429:'上游额度或速率受限，请查看账号用量后重试',
            502:'上游响应异常，请稍后重试',503:'上游暂不可用，请稍后重试',504:'上游超时，请稍后重试'}
    return f'{name}上游拒绝请求（HTTP {status}）；{advice.get(status,"请检查网络与订阅状态")}'


class SubscriptionStreamError(ValueError):
    def __init__(self, message, status):
        super().__init__(message)
        self.status=status


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def decoded(body, encoding):
    if encoding == 'zstd':
        try:
            import zstandard
        except ImportError:
            # codex/dsh 的请求体默认 zstd 压缩；缺库时给出可定位的错误，
            # 而不是让 ModuleNotFoundError 在请求线程里炸掉连接。
            raise ValueError('缺少 zstandard 依赖，无法解码 zstd 压缩请求体；请安装 zstandard 并重启网关')
        try:
            body = zstandard.ZstdDecompressor().decompress(body, max_output_size=MAX_DECODED)
        except Exception:
            # ZstdError 不在处理器捕获的异常类型里，统一转成可回复的 ValueError。
            raise ValueError('zstd 请求体解压失败') from None
    elif encoding == 'gzip':
        import io
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
            body = stream.read(MAX_DECODED + 1)
    elif encoding not in ('', 'identity'):
        raise ValueError('不支持的请求压缩格式')
    if len(body) > MAX_DECODED:
        raise ValueError('解压后的请求超过 128 MiB')
    return body


def session_id(headers, body):
    # Only explicit native identifiers; no temporal/text similarity grouping.
    for name in ('session_id','session-id','x-session-id','x-claude-code-session-id','x-codex-session-id','x-session-affinity','x-deepseek-harness-session-id'):
        if headers.get(name):
            return headers[name][:512]
    metadata = body.get('metadata') or {}
    value = metadata.get('user_id') if isinstance(metadata,dict) else None
    if isinstance(value,str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed,dict) and isinstance(parsed.get('session_id'),str):
                return parsed['session_id'][:512]
        except ValueError:
            # Older Claude versions use user_<device>_account_<id>_session_<uuid>.
            import re
            match = re.search(r'_session_([0-9a-fA-F-]{36})(?:$|_)',value)
            if match:
                return match[1]
    return None


def context_parts(body):
    parts = {}
    def add(kind, value):
        if value is None:
            return
        text = value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)
        parts[kind] = parts.get(kind,0) + sum(.25 if ord(c)<128 else 1 for c in text)
    add('system', body.get('system') or body.get('instructions'))
    add('tools', body.get('tools'))
    messages = body.get('messages') or body.get('input') or []
    if not isinstance(messages,list):
        add('messages',messages)
    else:
        for item in messages:
            if not isinstance(item,dict):
                add('messages',item)
                continue
            kind = 'tool_results' if item.get('type')=='function_call_output' or item.get('role')=='tool' else 'messages'
            # Files/memory are not inferred from message text.
            add(kind,item)
    return [{'kind':k,'tokens':max(1,round(v))} for k,v in parts.items() if v]


class Observation:
    def __init__(self, protocol):
        self.protocol = protocol
        self.fields = {}
        self.buffer = b''
        self.json_body = bytearray()
        self.truncated = False
        self.complete = False
        self.failed = False
        self.first = self.first_text = self.first_reasoning = self.last = None
        self.sse = False
        self.data_lines=[]
        self.data_bytes=0
        self.discard_event=False

    def usage(self, usage):
        if not isinstance(usage,dict) or not usage:
            return
        f = self.fields
        if self.protocol == 'anthropic':
            for target,source in [('in','input_tokens'),('out','output_tokens'),
                                  ('cache_read','cache_read_input_tokens'),('cache_write','cache_creation_input_tokens')]:
                if source in usage:
                    f[target] = usage[source]
            if 'input_tokens' in usage:
                f.setdefault('cache_read',0)
                f.setdefault('cache_write',0)
        else:
            total = usage.get('input_tokens',usage.get('prompt_tokens'))
            details = usage.get('input_tokens_details') or usage.get('prompt_tokens_details') or {}
            if total is not None:
                cached = details.get('cached_tokens',usage.get('cached_tokens',0))
                f.update({'in':max(0,total-cached),'cache_read':cached,'cache_write':0})
            output = usage.get('output_tokens',usage.get('completion_tokens'))
            if output is not None:
                f['out'] = output
            details = usage.get('output_tokens_details') or usage.get('completion_tokens_details') or {}
            if 'reasoning_tokens' in details:
                f['reasoning'] = details['reasoning_tokens']

    def event(self, event, ms):
        if not isinstance(event,dict):
            return
        f = self.fields
        t = event.get('type','')
        obj = event.get('response') or event.get('message') or event
        if isinstance(obj,dict):
            if obj.get('id'):
                f['response_id'] = obj['id']
            if obj.get('model'):
                f['served'] = obj['model']
            self.usage(obj.get('usage'))
        self.usage(event.get('usage'))
        text = reasoning = tool = False
        if t == 'content_block_delta':
            delta = event.get('delta') or {}
            text = bool(delta.get('text'))
            reasoning = bool(delta.get('thinking'))
            tool = bool(delta.get('partial_json'))
        if t == 'content_block_start':
            block = event.get('content_block') or {}
            tool = block.get('type')=='tool_use'
        if t.endswith('.delta'):
            text = t == 'response.output_text.delta' and bool(event.get('delta'))
            reasoning = 'reasoning' in t and bool(event.get('delta'))
            tool = ('function_call_arguments' in t or 'custom_tool_call_input' in t) and bool(event.get('delta'))
        if t == 'response.output_item.added':
            tool = (event.get('item') or {}).get('type') in ('function_call','custom_tool_call')
        for choice in event.get('choices') or []:
            delta = choice.get('delta') or choice.get('message') or {}
            text = text or bool(delta.get('content'))
            reasoning = reasoning or bool(delta.get('reasoning_content') or delta.get('reasoning'))
            tool = tool or bool(delta.get('tool_calls'))
            if choice.get('finish_reason'):
                f['finish_reason'] = choice['finish_reason']
        if text or reasoning or tool:
            if self.first is None:
                self.first = ms
            self.last = ms
        if text and self.first_text is None:
            self.first_text = ms
        if reasoning and self.first_reasoning is None:
            self.first_reasoning = ms
        if t in ('message_stop','response.completed'):
            self.complete = True
            f['stop'] = 'completed'
        if t in ('error','response.failed','response.incomplete') or event.get('error'):
            self.failed = True
            error = event.get('error') or obj.get('error') or {'message':t}
            f['err'] = json.dumps(error,ensure_ascii=False)[:4096]
            f['stop'] = 'error'

    def feed(self, chunk, ms):
        if not self.sse:
            if len(self.json_body)+len(chunk)<=MAX_CAPTURE:
                self.json_body.extend(chunk)
            else:
                self.truncated = True
            if bytes(self.json_body[:64]).lstrip().startswith((b'data:',b'event:')):
                self.sse=True
                pending=bytes(self.json_body)
                self.json_body.clear()
                self.feed(pending,ms)
            return
        self.buffer += chunk
        # An event line over the cap is ignored for observation, not forwarding.
        if len(self.buffer)>MAX_CAPTURE:
            self.buffer=b''
            self.truncated=True
            return
        while b'\n' in self.buffer:
            line,self.buffer = self.buffer.split(b'\n',1)
            line=line.rstrip(b'\r')
            if line.startswith(b'data:'):
                self.data_bytes+=len(line)
                if self.data_bytes>MAX_CAPTURE:
                    self.truncated=True;self.discard_event=True;self.data_lines=[]
                if not self.discard_event:self.data_lines.append(line[5:].lstrip(b' '))
                continue
            if not line:
                self.data_bytes=0
                if self.discard_event:
                    self.discard_event=False;continue
            if line or not self.data_lines:
                continue
            value=b'\n'.join(self.data_lines).strip()
            self.data_lines=[]
            if value == b'[DONE]':
                self.complete = True
                self.fields['stop'] = 'completed'
                continue
            try:
                self.event(json.loads(value),ms)
            except (ValueError,TypeError):
                pass

    def finish(self, ms):
        if not self.sse and self.json_body and not self.truncated:
            try:
                value = json.loads(self.json_body)
                self.event(value,ms)
                # A complete non-stream JSON response is its own completion marker.
                if isinstance(value,dict) and not self.failed and (value.get('choices') or value.get('type')=='message' or value.get('status')=='completed'):
                    self.complete=True
                    self.fields['stop']='completed'
            except (ValueError,TypeError):
                pass
        for key,value in [('ttft_ms',self.first),('first_text_ms',self.first_text),
                          ('first_reasoning_ms',self.first_reasoning)]:
            if value is not None:
                self.fields[key]=value
        if self.first is not None and self.last is not None:
            self.fields['flow_ms']=max(0,self.last-self.first)


class State:
    def __init__(self, directory, key, codex_base='https://chatgpt.com/backend-api/codex', codex_api='https://api.openai.com/v1', observation_root=None):
        self.directory = Path(directory)
        self.observation_root = Path(observation_root) if observation_root is not None else None
        self.key = key
        self.codex_base,self.codex_api=codex_base,codex_api
        self.lock=threading.RLock()
        self.recording=False
        self.generation=0
        self.last_prune=0
        self.active=0
        self.draining=False

    def providers(self):
        return json.loads((self.directory/'providers.json').read_text(encoding='utf-8')).get('providers',[])

    def consent(self, on, clear=False):
        with self.lock:
            self.generation+=1
            self.recording=bool(on)
            if clear:
                target=self.directory/'gateway-conversations'
                if target.exists():
                    if target.is_symlink() or target.resolve().parent!=self.directory.resolve():
                        raise ValueError('正文目录路径异常')
                    shutil.rmtree(target)

    def prune(self,force=False):
        if not force and time.time()-self.last_prune<3600:
            return
        self.last_prune=time.time()
        for name,days,limit in [('gateway-conversations',7,BODY_BYTES),('routing',30,ROUTE_BYTES)]:
            root=self.directory/name
            files=sorted((p for p in root.rglob('*.jsonl') if p.is_file() and not p.is_symlink()),key=lambda p:p.stat().st_mtime)
            size=sum(p.stat().st_size for p in files)
            for p in files:
                if p.stat().st_mtime<time.time()-days*86400 or size>limit:
                    size-=p.stat().st_size
                    p.unlink(missing_ok=True)

    def save(self, row, parts, capture, generation, sensitive=(), title_epoch=None, title_body=None):
        from agent_hub.observability.store import redact
        # Headers/account identities are never written. Known keys are removed
        # from provider error bodies and optional content before append.
        keys=[p['key'] for p in self.providers()]+[p.get('bindingKey','') for p in self.providers()]+[self.key]+list(sensitive)
        def clean(value):
            if isinstance(value,str):
                return redact(value,keys)
            if isinstance(value,list):
                return [clean(x) for x in value]
            if isinstance(value,dict):
                return {k:('[REDACTED]' if k.lower().replace('_','') in ('apikey','authorization','password','secret','accesstoken','refreshtoken','idtoken') else clean(v)) for k,v in value.items()}
            return value
        row=clean(row)
        with self.lock:
            self.prune()
            def append(path,obj):
                path.parent.mkdir(parents=True,exist_ok=True)
                with path.open('ab') as out:
                    out.write(packed(obj)+b'\n')
            append(self.directory/'usage.jsonl',row)
            total=sum(row.get(k) or 0 for k in ('in','cache_read','cache_write'))
            estimated=sum(p['tokens'] for p in parts)
            if total and estimated:
                parts=[dict(p,tokens=round(p['tokens']*total/estimated)) for p in parts]
            append(self.directory/'routing'/f'{row["t"][:10]}.jsonl',{
                'id':row['route_id'],'done':True,'engine':'agent-hub',
                'prompt':{'tokens':total or None,'counted':False,'parts':parts},'tries':[{'status':row['status']}]})
            if capture and row.get('session') and self.recording and generation==self.generation:
                def clipped(value):
                    if isinstance(value,str):return len(value)>16000
                    if isinstance(value,dict):return any(clipped(v) for v in value.values())
                    if isinstance(value,list):return any(clipped(v) for v in value)
                    return False
                capture['cut']=capture.get('cut',False) or clipped(capture)
                digest=hashlib.sha256(packed([row['agent'],row['session']])).hexdigest()
                append(self.directory/'gateway-conversations'/row['t'][:10]/digest/'turns.jsonl',
                       clean(dict(capture,agent=row['agent'],session=row['session'],time=row['t'],model=row.get('req'),status=row['status'])))
            self.prune(force=True)
        if title_epoch is not None:
            # Text is kept out of the append-only usage and routing ledgers.
            from agent_hub.sessions.gateway_titles import save as save_title
            from agent_hub.observability.store import stamp
            started = stamp(row.get('t'))
            if started is not None:
                sid = row.get('session')
                if sid and row.get('agent') == 'claude' and row.get('subagent'):
                    sid += '/agent-' + row['subagent']
                save_title(self.observation_root, row.get('agent'), sid,
                           title_body, started, title_epoch, keys)


class Server(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=False


class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'

    def log_message(self,*args):
        pass # URLs/headers may contain credentials; no HTTP access log.

    def reply(self, status, value):
        body=packed(value)
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Connection','close')
        self.close_connection=True
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def handle_request(self):
        state=self.server.state
        # Browser origins cannot initiate calls with cookies or native credentials.
        if self.headers.get('Origin'):
            return self.reply(403,{'error':{'message':'仅允许本机 Agent 客户端调用'}})
        if getattr(self.server,'control',False):
            if self.path not in ('/recording','/drain') or self.command!='POST' or not secrets.compare_digest(self.headers.get('Authorization',''),'Bearer '+state.key):
                return self.reply(403,{'error':'forbidden'})
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=1024:raise ValueError('invalid control body')
                opts=json.loads(self.rfile.read(size))
                if self.path=='/drain':
                    with state.lock:state.draining=True
                    deadline=time.monotonic()+5
                    while state.active and time.monotonic()<deadline:time.sleep(.02)
                    return self.reply(200,{'active':state.active})
                state.consent(bool(opts.get('on')),bool(opts.get('clear')))
                return self.reply(200,{'recording':state.recording})
            except (ValueError,OSError):
                return self.reply(400,{'error':'invalid control request'})
        if self.headers.get('Upgrade','').lower()=='websocket':
            return self.reply(426,{'error':{'message':'HTTP SSE required'}})
        if state.draining:
            return self.reply(503,{'error':{'message':'Hub 网关正在退出'}})
        path=urllib.parse.urlsplit(self.path)
        segments=path.path.strip('/').split('/')
        if segments[0]=='workbuddy':
            return self.reply(410,{'error':{'message':'WorkBuddy 订阅反代已取消；请在 Hub 中切换其他连接'}})
        special_kinds={'codex-subscription':'codex-chatgpt','zcode-subscription':'zcode-subscription'}
        special_kind=special_kinds.get(segments[0]) if len(segments)>2 else None
        special_agent=segments[1] if special_kind and segments[1] in ('claude','codex','pi','dsh','opencode') else None
        if special_agent:
            path=path._replace(path='/'+('/'.join(segments[2:])))
        native=path.path.startswith('/backend-api/codex/')
        if self.command=='GET' and path.path in ('/v1/models','/models'):
            mapping=json.loads((state.directory/'hub-connections.json').read_text(encoding='utf-8')) if special_agent else {}
            rows=[{'id':m if special_agent else p['id']+'/'+m,'object':'model'} for p in state.providers()
                  if p.get('kind')!='workbuddy-ai' and (not special_agent or p.get('kind')==special_kind and mapping.get(p['id'],{}).get('agent')==special_agent) for m in p['models']]
            return self.reply(200,{'object':'list','data':rows})
        if self.command not in ('GET','POST') or not native and path.path not in ('/v1/messages','/v1/chat/completions','/v1/responses','/v1/responses/compact'):
            return self.reply(404,{'error':{'message':'unknown gateway endpoint'}})
        try:
            length=int(self.headers.get('Content-Length','0'))
            if self.headers.get('Transfer-Encoding') or not 0<=length<=MAX_BODY:
                return self.reply(413,{'error':{'message':'request body limit / Content-Length required'}})
            wire=self.rfile.read(length)
            body=json.loads(decoded(wire,self.headers.get('Content-Encoding',''))) if wire else {}
            if not isinstance(body,dict):raise ValueError('expected object')
        except (ValueError,OSError) as exc:
            return self.reply(400,{'error':{'message':str(exc) or 'invalid JSON or compressed body'}})
        model=body.get('model','')
        providers=state.providers()
        provider=next((p for p in providers if isinstance(model,str) and model.startswith(p['id']+'/')),None)
        if special_agent:
            mapping=json.loads((state.directory/'hub-connections.json').read_text(encoding='utf-8'))
            provider=next((p for p in providers if p.get('kind')==special_kind and mapping.get(p['id'],{}).get('agent')==special_agent),None)
        if provider and provider.get('kind')=='workbuddy-ai':
            return self.reply(410,{'error':{'message':'WorkBuddy 订阅反代已取消；请在 Hub 中切换其他连接'}})
        headers={k:v for k,v in self.headers.items() if k.lower() not in HOP and k.lower() not in ('accept-encoding',)}
        headers['Accept-Encoding']='identity'
        protocol='responses' if native or '/responses' in path.path else 'anthropic' if path.path.endswith('/messages') else 'chat'
        if provider:
            mapping=json.loads((state.directory/'hub-connections.json').read_text(encoding='utf-8'))
            agent=mapping[provider['id']]['agent']
            token=self.headers.get('x-api-key') or self.headers.get('Authorization','').removeprefix('Bearer ')
            allowed_tokens = (provider['key'],) if provider.get('kind') in ('codex-chatgpt','zcode-subscription') else ('agent-hub-'+agent,'magpie-'+agent,provider['key'])
            if not any(secrets.compare_digest(token,allowed) for allowed in allowed_tokens):
                return self.reply(401,{'error':{'message':'invalid local agent token'}})
            upstream_model=model if special_agent else model[len(provider['id'])+1:]
            if provider.get('kind')=='zcode-subscription' and isinstance(provider.get('subscriptionModels'),list) and upstream_model not in provider['subscriptionModels']:
                return self.reply(400,{'error':{'message':'该模型不在当前 ZCode 套餐目录中，请刷新模型或选择其他模型'}})
            if upstream_model not in provider['models']:
                return self.reply(400,{'error':{'message':'model not configured on this connection'}})
            if provider.get('kind')=='codex-chatgpt':
                if self.command!='POST' or path.path.endswith('/compact'):
                    return self.reply(400,{'error':{'message':'Codex 订阅不支持此端点；请发送完整消息进行推理'}})
                return self.forward_codex_subscription(body,agent,provider,model,upstream_model,protocol)
            if provider.get('kind')=='zcode-subscription':
                if self.command!='POST' or path.path.endswith('/compact'):
                    return self.reply(400,{'error':{'message':'ZCode 订阅不支持此端点；请发送完整消息进行推理'}})
                return self.forward_zcode_subscription(body,agent,provider,model,upstream_model,protocol)
            if protocol not in provider:
                return self.reply(400,{'error':{'message':'connection protocol mismatch'}})
            suffix={'anthropic':'/messages','responses':'/responses/compact' if path.path.endswith('/compact') else '/responses','chat':'/chat/completions'}[protocol]
            base=provider[protocol].rstrip('/')
            # Anthropic base URLs conventionally omit /v1; OpenAI bases include it.
            if protocol=='anthropic' and not base.endswith('/v1'):
                base+='/v1'
            upstream=base+suffix
            body['model']=upstream_model
            wire=packed(body)
            headers={k:v for k,v in headers.items() if k.lower() not in ('authorization','x-api-key','content-encoding','cookie','chatgpt-account-id')}
            headers['Authorization']='Bearer '+provider['key']
            if protocol=='anthropic':headers['x-api-key']=provider['key']
            route_provider=provider['id']
        elif native:
            auth=self.headers.get('Authorization','')
            if not auth.startswith('Bearer ') or auth.split(' ',1)[1].startswith(('magpie-','agent-hub-')):
                return self.reply(401,{'error':{'message':'native Codex authorization required'}})
            agent='codex'
            # Native backend owns login/refresh. Forward bearer/account headers
            # in memory; never discover, copy or persist auth.json.
            base=state.codex_api if auth[7:].startswith('sk-') and path.path.split('/')[-1]!='models' else state.codex_base
            upstream=base+path.path[len('/backend-api/codex'):]
            route_provider=None # Account/provider identity is intentionally unknown.
        else:
            return self.reply(400,{'error':{'message':'explicit configured connection namespace required'}})
        if path.query:
            upstream+='?'+path.query
        self.forward(upstream,headers,wire,body,agent,route_provider,model,protocol)

    def forward_codex_subscription(self, body, agent, provider, model, upstream_model, protocol):
        from codex_subscription import (CodexSubscriptionError, ResponsesToChat, client_version, stream_error, stream_status,
                                        headers as codex_headers, read_auth, request_body)
        from agent_hub.gateway.protocol import Reply, encode_event, sse_events
        import queue
        import select
        import socket
        state=self.server.state
        start=time.monotonic();sid=session_id(self.headers,body)
        row={'engine':'agent-hub','schema':1,'t':now(),'agent':agent,'session':sid,
             'provider':provider['id'],'req':model,'model':model,'route_id':uuid.uuid4().hex,'tries':1}
        from agent_hub.sessions.gateway_titles import begin as begin_title
        title_epoch=begin_title(state.observation_root) if sid else None
        with state.lock:
            generation=state.generation;capture_on=state.recording;state.active+=1
        observer=Observation(protocol);parts=context_parts(body)
        streaming=body.get('stream',False) is True
        sent=False;validated=False;captured=bytearray();sensitive=[];reply=None
        done=threading.Event();cancelled=threading.Event();sockets=[]
        try:
            request,names=request_body(body,protocol,upstream_model)
            auth=read_auth(provider.get('authFile'))
            sensitive=[auth['access'],auth['account']]
            version=client_version(Path(provider['authFile']).parent)
            headers={**codex_headers(auth,version),'Content-Type':'application/json',
                     'Accept':'text/event-stream','Accept-Encoding':'identity'}
            validated=True
            incoming=queue.Queue(maxsize=16)
            def publish(kind,value=None):
                while not done.is_set():
                    try:incoming.put((kind,value),timeout=.1);return
                    except queue.Full:pass
            def read_upstream():
                connection=None
                try:
                    connection,target,proxy_headers=connection_for(state.codex_base+'/responses',timeout=180)
                    connection.connect();sockets.append(connection.sock)
                    if cancelled.is_set():raise CodexSubscriptionError('客户端已取消 Codex 订阅请求')
                    connection.request('POST',target,packed(request),{**headers,**proxy_headers})
                    response=connection.getresponse()
                    publish('headers',(response.status,response.getheader('Content-Type','')))
                    if response.status<400:
                        for event in sse_events(response,'Codex 订阅',allow_errors=True):
                            if done.is_set():break
                            publish('event',event)
                    else:
                        response.read(1024*1024) # never expose the vendor body or credentials
                    publish('done')
                except Exception as exc:publish('error',exc)
                finally:
                    if connection is not None:
                        try:connection.close()
                        except OSError:pass
            def watch_client():
                while not done.wait(.1):
                    try:
                        readable,_,_=select.select([self.connection],[],[],0)
                        if readable and not self.connection.recv(1,socket.MSG_PEEK):
                            cancelled.set()
                            for sock in sockets:
                                try:sock.shutdown(socket.SHUT_RDWR)
                                except OSError:pass
                            break
                    except OSError:
                        cancelled.set()
                        for sock in sockets:
                            try:sock.shutdown(socket.SHUT_RDWR)
                            except OSError:pass
                        break
            threading.Thread(target=watch_client,daemon=True).start()
            threading.Thread(target=read_upstream,daemon=True).start()
            def next_upstream():
                while True:
                    if cancelled.is_set():raise CodexSubscriptionError('客户端已取消 Codex 订阅请求')
                    try:kind,value=incoming.get(timeout=.1)
                    except queue.Empty:continue
                    if kind=='error':raise value
                    return kind,value
            kind,meta=next_upstream()
            if kind!='headers':raise CodexSubscriptionError('Codex 订阅未返回响应头')
            status,content_type=meta;row['status']=status
            if status>=400:
                row['err']=subscription_failure('Codex 订阅',status)
                self.reply(status,{'error':{'message':row['err'],'type':'upstream_error'}})
                return
            # The Codex backend can deliver a valid SSE stream without a MIME
            # header; an explicitly different content type is still rejected.
            if content_type and 'text/event-stream' not in content_type.lower():
                raise CodexSubscriptionError('Codex 订阅未返回流式响应')
            observer.sse=streaming
            bridge=ResponsesToChat() if protocol!='responses' else None
            reply=Reply(protocol,model,names) if bridge else None
            final_response=None;items={}
            def send(values):
                nonlocal sent
                if not streaming:return
                if values and not sent:
                    self.send_response(200);self.send_header('Content-Type','text/event-stream; charset=utf-8')
                    self.send_header('Cache-Control','no-cache');self.send_header('Connection','close')
                    self.end_headers();self.close_connection=True;sent=True
                for value in values:
                    wire=encode_event(protocol,value)
                    self.wfile.write(wire);self.wfile.flush()
                    observer.feed(wire,round((time.monotonic()-start)*1000,2))
                    if capture_on:captured.extend(wire[:max(0,MAX_CAPTURE-len(captured))])
            while True:
                kind,event=next_upstream()
                if kind=='done':break
                if kind!='event':continue
                if not isinstance(event,dict):raise ValueError('Codex 订阅流格式无效')
                if event.get('type') in ('response.failed','error') or event.get('error'):
                    raise SubscriptionStreamError(stream_error(event),stream_status(event))
                if bridge:
                    for chunk in bridge.feed(event):
                        # An Anthropic thinking block needs a provider signature;
                        # a Codex reasoning summary cannot be used as one.
                        if protocol=='anthropic' and any('reasoning_content' in (choice.get('delta') or {}) for choice in chunk.get('choices') or []):
                            continue
                        send(reply.feed(chunk))
                else:
                    typ=event.get('type')
                    if typ=='response.output_item.done':items[event.get('output_index',len(items))]=event.get('item')
                    if typ in ('response.completed','response.incomplete'):
                        final_response=event.get('response') or {}
                        if typ=='response.incomplete' and (final_response.get('incomplete_details') or {}).get('reason')!='max_output_tokens':
                            raise ValueError('Codex 订阅上游响应未完成')
                        if not final_response.get('output') and items:
                            final_response={**final_response,'output':[items[i] for i in sorted(items)]}
                        event={**event,'response':final_response}
                    if isinstance(event.get('response'),dict):
                        event={**event,'response':{**event['response'],'model':model}}
                    send([event])
            if bridge:
                if not bridge.finished:raise ValueError('Codex 订阅流提前结束，未收到完成标记')
                send(reply.end())
                if streaming and protocol=='chat':
                    self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush()
                    observer.feed(b'data: [DONE]\n\n',round((time.monotonic()-start)*1000,2))
                result=reply.complete()
            else:
                if final_response is None:raise ValueError('Codex 订阅流提前结束，未收到完成标记')
                result={**final_response,'model':model}
            if not streaming:
                wire=packed(result);self.reply(200,result);sent=True
                observer.feed(wire,round((time.monotonic()-start)*1000,2))
                if capture_on:captured.extend(wire[:MAX_CAPTURE])
            observer.finish(round((time.monotonic()-start)*1000,2))
            row['served']=upstream_model
        except (BrokenPipeError,ConnectionResetError):
            row.update(status=499,stop='aborted',err='客户端连接中断；已取消 Codex 订阅请求')
        except (ValueError,OSError,http.client.HTTPException,TypeError,KeyError,AttributeError) as exc:
            status=499 if cancelled.is_set() else exc.status if isinstance(exc,SubscriptionStreamError) else 401 if isinstance(exc,CodexSubscriptionError) and not validated else 502 if validated or isinstance(exc,ProxyError) else 400
            message=str(exc) if isinstance(exc,ValueError) else 'Codex 订阅上游连接或响应读取失败'
            row.update(status=status,stop='aborted' if status==499 else 'error',err=message)
            try:
                if not sent and status!=499:self.reply(status,{'error':{'message':message}})
                elif sent and streaming and status!=499:
                    value={'type':'error','error':{'type':'upstream_error','message':message}}
                    if protocol=='responses':value={'type':'response.failed','response':{'status':'failed','error':{'code':'upstream_error','message':message}}}
                    elif protocol=='chat':value={'error':{'message':message}}
                    self.wfile.write(encode_event(protocol,value));self.wfile.flush()
            except OSError:pass
        finally:
            done.set()
            if row.get('status',200)>=400:
                for sock in sockets:
                    try:sock.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
            row={**observer.fields,**row};row['ms']=round((time.monotonic()-start)*1000,2)
            if reply:
                if 'prompt_tokens' not in reply.usage:
                    for key in ('in','cache_read','cache_write'):row.pop(key,None)
                if 'completion_tokens' not in reply.usage:row.pop('out',None)
                if 'reasoning_tokens' not in (reply.usage.get('completion_tokens_details') or {}):row.pop('reasoning',None)
            capture={'input':body,'output':captured.decode('utf-8',errors='replace'),'cut':len(captured)>=MAX_CAPTURE} if capture_on and sid else None
            try:state.save(row,parts,capture,generation,sensitive,title_epoch,body)
            except Exception:print('gateway observation write failed',flush=True)
            with state.lock:state.active-=1

    def forward_zcode_subscription(self, body, agent, provider, model, upstream_model, protocol):
        from zcode_subscription import (ZCodeSubscriptionError, AnthropicToChat, AnthropicCollector,
                                        binding, read_auth, resolve_auth, request_body, start_request, _current_identity,
                                        stream_error, stream_status)
        from agent_hub.gateway.protocol import Reply, encode_event, sse_events
        import queue
        import select
        import socket
        state=self.server.state
        start=time.monotonic();sid=session_id(self.headers,body)
        row={'engine':'agent-hub','schema':1,'t':now(),'agent':agent,'session':sid,
             'provider':provider['id'],'req':model,'model':model,'route_id':uuid.uuid4().hex,'tries':1}
        from agent_hub.sessions.gateway_titles import begin as begin_title
        title_epoch=begin_title(state.observation_root) if sid else None
        with state.lock:
            generation=state.generation;capture_on=state.recording;state.active+=1
        observer=Observation(protocol);parts=context_parts(body)
        streaming=body.get('stream',False) is True
        sent=False;validated=False;captured=bytearray();sensitive=[];reply=None
        done=threading.Event();cancelled=threading.Event();sockets=[]
        try:
            request,names=request_body(body,protocol,upstream_model)
            auth=read_auth(provider.get('zcodeHome'))
            if provider.get('subscriptionBinding') and not secrets.compare_digest(provider['subscriptionBinding'],binding(auth,provider.get('bindingKey',''))):
                raise ZCodeSubscriptionError('ZCode 账号或套餐已切换；请在设置中刷新模型并恢复服务')
            resolved=resolve_auth(auth)
            if _current_identity(auth)!=_current_identity(read_auth(provider.get('zcodeHome'))):
                raise ZCodeSubscriptionError('读取套餐期间 ZCode 账号已切换；请刷新后重试')
            auth=resolved
            sensitive=[auth.get('key',''),auth.get('token',''),auth.get('jwt',''),auth['account']]
            if auth.get('kind')=='start-plan':
                request,headers=start_request(auth,request,sid)
            else:
                headers={'x-api-key':auth['key'],'Authorization':'Bearer '+auth['key'],
                         'anthropic-version':'2023-06-01'}
                if sid:headers['x-session-id']=sid
            headers.update({'Content-Type':'application/json','Accept':'text/event-stream','Accept-Encoding':'identity'})
            validated=True
            incoming=queue.Queue(maxsize=16)
            def publish(kind,value=None):
                while not done.is_set():
                    try:incoming.put((kind,value),timeout=.1);return
                    except queue.Full:pass
            def read_upstream():
                connection=None
                try:
                    connection,target,proxy_headers=connection_for(auth['base']+'/v1/messages',timeout=180)
                    connection.connect();sockets.append(connection.sock)
                    if cancelled.is_set():raise ZCodeSubscriptionError('客户端已取消 ZCode 订阅请求')
                    connection.request('POST',target,packed(request),{**headers,**proxy_headers})
                    response=connection.getresponse()
                    content_type=response.getheader('Content-Type','')
                    status=response.status
                    if status>=400 or 'application/json' in content_type.lower():
                        raw=response.read(1024*1024)
                        if status<400:
                            try:
                                value=json.loads(raw);code=str(value.get('code') or (value.get('error') or {}).get('code') or '')
                            except (ValueError,TypeError,AttributeError):code=''
                            status=429 if code in ('1005','1113','1302','1303') else 502
                        publish('headers',(status,content_type));publish('done');return
                    publish('headers',(status,content_type))
                    if status<400:
                        for event in sse_events(response,'ZCode 订阅',allow_errors=True):
                            if done.is_set():break
                            publish('event',event)
                    publish('done')
                except Exception as exc:publish('error',exc)
                finally:
                    if connection is not None:
                        try:connection.close()
                        except OSError:pass
            def watch_client():
                while not done.wait(.1):
                    try:
                        readable,_,_=select.select([self.connection],[],[],0)
                        if readable and not self.connection.recv(1,socket.MSG_PEEK):
                            cancelled.set()
                            for sock in sockets:
                                try:sock.shutdown(socket.SHUT_RDWR)
                                except OSError:pass
                            break
                    except OSError:
                        cancelled.set()
                        for sock in sockets:
                            try:sock.shutdown(socket.SHUT_RDWR)
                            except OSError:pass
                        break
            threading.Thread(target=watch_client,daemon=True).start()
            threading.Thread(target=read_upstream,daemon=True).start()
            def next_upstream():
                while True:
                    if cancelled.is_set():raise ZCodeSubscriptionError('客户端已取消 ZCode 订阅请求')
                    try:kind,value=incoming.get(timeout=.1)
                    except queue.Empty:continue
                    if kind=='error':raise value
                    return kind,value
            kind,meta=next_upstream()
            if kind!='headers':raise ZCodeSubscriptionError('ZCode 订阅未返回响应头')
            status,content_type=meta;row['status']=status
            if status>=400:
                row['err']=subscription_failure('ZCode 订阅',status)
                self.reply(status,{'error':{'message':row['err'],'type':'upstream_error'}})
                return
            if content_type and 'text/event-stream' not in content_type.lower():
                raise ZCodeSubscriptionError('ZCode 订阅未返回流式响应')
            observer.sse=streaming
            bridge=AnthropicToChat() if protocol!='anthropic' else None
            reply=Reply(protocol,model,names) if bridge else None
            collector=AnthropicCollector(model) if not bridge else None
            def send(values):
                nonlocal sent
                if not streaming:return
                if values and not sent:
                    self.send_response(200);self.send_header('Content-Type','text/event-stream; charset=utf-8')
                    self.send_header('Cache-Control','no-cache');self.send_header('Connection','close')
                    self.end_headers();self.close_connection=True;sent=True
                for value in values:
                    wire=encode_event(protocol,value)
                    self.wfile.write(wire);self.wfile.flush()
                    observer.feed(wire,round((time.monotonic()-start)*1000,2))
                    if capture_on:captured.extend(wire[:max(0,MAX_CAPTURE-len(captured))])
            while True:
                kind,event=next_upstream()
                if kind=='done':break
                if kind!='event':continue
                if not isinstance(event,dict):raise ValueError('ZCode 订阅流格式无效')
                if event.get('type')=='error' or event.get('error') or event.get('code'):
                    raise SubscriptionStreamError(stream_error(event),stream_status(event))
                if bridge:
                    for chunk in bridge.feed(event):send(reply.feed(chunk))
                else:
                    collector.feed(event)
                    if isinstance(event.get('message'),dict):
                        event={**event,'message':{**event['message'],'model':model}}
                    send([event])
            if bridge:
                if not bridge.finished:raise ValueError('ZCode 订阅流提前结束，未收到完成标记')
                send(reply.end())
                if streaming and protocol=='chat':
                    self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush()
                    observer.feed(b'data: [DONE]\n\n',round((time.monotonic()-start)*1000,2))
                result=reply.complete()
            else:
                if not collector.finished:raise ValueError('ZCode 订阅流提前结束，未收到完成标记')
                result=collector.value
            if not streaming:
                wire=packed(result);self.reply(200,result);sent=True
                observer.feed(wire,round((time.monotonic()-start)*1000,2))
                if capture_on:captured.extend(wire[:MAX_CAPTURE])
            observer.finish(round((time.monotonic()-start)*1000,2))
            row['served']=upstream_model
        except (BrokenPipeError,ConnectionResetError):
            row.update(status=499,stop='aborted',err='客户端连接中断；已取消 ZCode 订阅请求')
        except (ValueError,OSError,http.client.HTTPException,TypeError,KeyError,AttributeError) as exc:
            status=499 if cancelled.is_set() else exc.status if isinstance(exc,SubscriptionStreamError) else 401 if isinstance(exc,ZCodeSubscriptionError) and not validated else 502 if validated or isinstance(exc,ProxyError) else 400
            message=str(exc) if isinstance(exc,ValueError) else 'ZCode 订阅上游连接或响应读取失败'
            row.update(status=status,stop='aborted' if status==499 else 'error',err=message)
            try:
                if not sent and status!=499:self.reply(status,{'error':{'message':message}})
                elif sent and streaming and status!=499:
                    value={'type':'error','error':{'type':'upstream_error','message':message}}
                    if protocol=='responses':value={'type':'response.failed','response':{'status':'failed','error':{'code':'upstream_error','message':message}}}
                    elif protocol=='chat':value={'error':{'message':message}}
                    self.wfile.write(encode_event(protocol,value));self.wfile.flush()
            except OSError:pass
        finally:
            done.set()
            if row.get('status',200)>=400:
                for sock in sockets:
                    try:sock.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
            row={**observer.fields,**row};row['ms']=round((time.monotonic()-start)*1000,2)
            if reply:
                if 'prompt_tokens' not in reply.usage:
                    for key in ('in','cache_read','cache_write'):row.pop(key,None)
                if 'completion_tokens' not in reply.usage:row.pop('out',None)
                if 'reasoning_tokens' not in (reply.usage.get('completion_tokens_details') or {}):row.pop('reasoning',None)
            capture={'input':body,'output':captured.decode('utf-8',errors='replace'),'cut':len(captured)>=MAX_CAPTURE} if capture_on and sid else None
            try:state.save(row,parts,capture,generation,sensitive,title_epoch,body)
            except Exception:print('gateway observation write failed',flush=True)
            with state.lock:state.active-=1


    def forward(self, upstream, headers, wire, body, agent, provider, model, protocol):
        state=self.server.state
        start=time.monotonic()
        with state.lock:
            generation=state.generation
            capture_on=state.recording
            state.active+=1
        observer=Observation(protocol)
        sid=session_id(self.headers,body)
        row={'engine':'agent-hub','schema':1,'t':now(),'agent':agent,'session':sid,
             'provider':provider,'req':model,'model':model,'route_id':uuid.uuid4().hex,'tries':1}
        from agent_hub.sessions.gateway_titles import begin as begin_title
        title_epoch=begin_title(state.observation_root) if sid else None
        if agent=='claude' and self.headers.get('x-claude-code-agent-id'):
            row['subagent']=self.headers['x-claude-code-agent-id'][:128]
            row['parent_subagent']=self.headers.get('x-claude-code-parent-agent-id','')[:128] or None
        if self.path.split('?',1)[0].endswith('/compact') or self.headers.get('x-deepseek-harness-compact','').lower() not in ('','0','false','no','off'):
            row['kind']='compaction'
        parts=context_parts(body)
        conn=None
        sent=False
        response_bytes=bytearray()
        cut=False
        inference=self.command=='POST' and any(x in self.path for x in ('/responses','/messages','/chat/completions'))
        try:
            url=urllib.parse.urlsplit(upstream)
            if url.scheme not in ('http','https') or not url.hostname or url.username or url.password:
                raise ValueError('invalid upstream URL')
            conn,target,proxy_headers=connection_for(url,timeout=180)
            conn.request(self.command,target,body=wire if self.command=='POST' else None,headers={**headers,**proxy_headers})
            response=conn.getresponse()
            if self.command=='GET' and self.path.split('?',1)[0]=='/backend-api/codex/models':
                content=response.read(MAX_BODY)
                try:models=json.loads(decoded(content,response.getheader('Content-Encoding','')))
                except (ValueError,OSError):models={}
                if response.status!=200 or not isinstance(models.get('models'),list):
                    cache=state.directory.parent.parent/'home/.codex/models_cache.json'
                    try:models=json.loads(cache.read_text(encoding='utf-8'))
                    except (ValueError,OSError):models={}
                if isinstance(models.get('models'),list):
                    return self.reply(200,{'models':models['models']})
                return self.reply(502,{'error':{'message':'上游模型目录不可用，且没有有效的本机目录缓存'}})
            row['status']=response.status
            row['responseType']=response.getheader('Content-Type','')[:100]
            observer.sse='text/event-stream' in row['responseType'].lower()
            encoding=response.getheader('Content-Encoding','identity').lower()
            row['responseEncoding']=encoding
            decoder=None
            if encoding=='gzip':
                import zlib
                decoder=zlib.decompressobj(16+zlib.MAX_WBITS)
            elif encoding=='zstd':
                import zstandard
                decoder=zstandard.ZstdDecompressor().decompressobj()
            encoded=encoding not in ('','identity','gzip','zstd')
            if encoded:row['observationGap']='该响应压缩格式尚未支持观测；原始响应仍透传'
            self.send_response(response.status)
            excluded=HOP | {x.strip().lower() for x in response.getheader('Connection','').split(',')}
            for k,v in response.getheaders():
                if k.lower() not in excluded and k.lower()!='set-cookie':
                    self.send_header(k,v)
            self.send_header('Connection','close')
            self.end_headers()
            self.close_connection=True
            sent=True
            while True:
                chunk=response.read1(65536)
                if not chunk:break
                ms=round((time.monotonic()-start)*1000,2)
                self.wfile.write(chunk)
                self.wfile.flush()
                try:
                    inspected=decoder.decompress(chunk) if decoder else chunk
                except Exception:
                    # Decoding is solely for observation; original encoded bytes
                    # have already reached the client and must keep flowing.
                    encoded=True;decoder=None;inspected=b''
                    row['observationGap']='响应解压采集失败；转发未改写'
                row['responseBytes']=row.get('responseBytes',0)+len(chunk)
                if not encoded:
                    try:observer.feed(inspected,ms)
                    except (ValueError,TypeError,AttributeError,KeyError):
                        row['observationGap']='响应观测字段无法解析；原始流继续转发'
                if capture_on:
                    room=MAX_CAPTURE-len(response_bytes)
                    response_bytes.extend(inspected[:room])
                    cut=cut or len(inspected)>room
            try:observer.finish(round((time.monotonic()-start)*1000,2))
            except (ValueError,TypeError,AttributeError,KeyError):
                row['observationGap']='响应观测字段无法解析；原始流已转发'
            if response.status>=400:
                row['err']='upstream HTTP '+str(response.status)
                if observer.json_body:
                    try:row['err']=json.dumps(json.loads(observer.json_body),ensure_ascii=False)[:4096]
                    except ValueError:pass
            elif inference and observer.sse and not observer.complete and not observer.failed and not encoded:
                row.update(status=499,stop='aborted',err='上游流结束但未收到协议完成标记')
        except (BrokenPipeError,ConnectionResetError):
            row.update(status=499,stop='aborted',err='客户端或上游连接中断；已停止本次转发')
        except (OSError,ValueError,http.client.HTTPException) as exc:
            row.update(status=502,stop='error',err=str(exc) if isinstance(exc,ProxyError) else '上游连接、TLS 或响应读取失败（'+type(exc).__name__+'）')
            if not sent:
                try:self.reply(502,{'error':{'message':row['err']}})
                except OSError:pass
        finally:
            if conn:conn.close()
            if inference:
                fields=observer.fields
                row={**fields,**row} # Transport interruption wins over partial completion.
                row['ms']=round((time.monotonic()-start)*1000,2)
                capture=None
                if capture_on and sid:
                    capture={'input':body,'output':response_bytes.decode('utf-8',errors='replace'),'cut':cut}
                sensitive=[self.headers.get('Authorization','').removeprefix('Bearer '),self.headers.get('x-api-key',''),self.headers.get('chatgpt-account-id','')]
                try:state.save(row,parts,capture,generation,sensitive,title_epoch,body)
                except Exception:
                    # Never leak headers/body to stderr or break a successful stream
                    # because observation storage is unavailable.
                    print('gateway observation write failed',flush=True)
            with state.lock:state.active-=1


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--port',type=int,required=True)
    parser.add_argument('--control-port',type=int,required=True)
    parser.add_argument('--observation-root',type=Path)
    args=parser.parse_args()
    state=State(args.directory,os.environ.pop('AGENT_HUB_GATEWAY_CONTROL_KEY'),observation_root=args.observation_root)
    server=Server(('127.0.0.1',args.port),Handler)
    control=Server(('127.0.0.1',args.control_port),Handler)
    server.state=control.state=state
    control.control=True
    threading.Thread(target=control.serve_forever,daemon=True).start()
    try:server.serve_forever()
    finally:
        server.server_close()
        control.shutdown()
        control.server_close()


if __name__=='__main__':
    main()
