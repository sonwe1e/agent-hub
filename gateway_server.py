"""Agent Hub's own loopback gateway. No external engine or account discovery.

Wire bytes are forwarded as they arrive. Observation is a bounded side reader;
ordinary connections keep their wire protocol. WorkBuddy has an explicit adapter.
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

MAX_BODY = 32 * 1024 * 1024
MAX_DECODED = 128 * 1024 * 1024
MAX_CAPTURE = 8 * 1024 * 1024
BODY_BYTES = 256 * 1024 * 1024
ROUTE_BYTES = 200 * 1024 * 1024
HOP = {'connection','keep-alive','proxy-authenticate','proxy-authorization',
       'te','trailer','transfer-encoding','upgrade','host','content-length'}


def packed(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def decoded(body, encoding):
    if encoding == 'zstd':
        import zstandard
        body = zstandard.ZstdDecompressor().decompress(body, max_output_size=MAX_DECODED)
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
    def __init__(self, directory, key, codex_base='https://chatgpt.com/backend-api/codex', codex_api='https://api.openai.com/v1'):
        self.directory = Path(directory)
        self.key = key
        self.codex_base,self.codex_api=codex_base,codex_api
        self.lock=threading.RLock()
        self.recording=False
        self.generation=0
        self.last_prune=0
        self.active=0
        self.draining=False
        self.workbuddy_salt=secrets.token_hex(16)
        self.workbuddy_history={}

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

    def save(self, row, parts, capture, generation, sensitive=()):
        from observation import redact
        # Headers/account identities are never written. Known keys are removed
        # from provider error bodies and optional content before append.
        keys=[p['key'] for p in self.providers()]+[self.key]+list(sensitive)
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
        wb_agent=segments[1] if len(segments)>2 and segments[0]=='workbuddy' and segments[1] in ('claude','codex','pi','dsh') else None
        if wb_agent:
            path=path._replace(path='/'+('/'.join(segments[2:])))
        native=path.path.startswith('/backend-api/codex/')
        if self.command=='GET' and path.path in ('/v1/models','/models'):
            mapping=json.loads((state.directory/'hub-connections.json').read_text(encoding='utf-8')) if wb_agent else {}
            rows=[{'id':m if wb_agent else p['id']+'/'+m,'object':'model'} for p in state.providers()
                  if not wb_agent or p.get('kind')=='workbuddy-ai' and mapping.get(p['id'],{}).get('agent')==wb_agent for m in p['models']]
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
        except (ValueError,OSError):
            return self.reply(400,{'error':{'message':'invalid JSON or compressed body'}})
        model=body.get('model','')
        providers=state.providers()
        provider=next((p for p in providers if isinstance(model,str) and model.startswith(p['id']+'/')),None)
        if wb_agent:
            mapping=json.loads((state.directory/'hub-connections.json').read_text(encoding='utf-8'))
            provider=next((p for p in providers if p.get('kind')=='workbuddy-ai' and mapping.get(p['id'],{}).get('agent')==wb_agent),None)
        headers={k:v for k,v in self.headers.items() if k.lower() not in HOP and k.lower() not in ('accept-encoding',)}
        headers['Accept-Encoding']='identity'
        protocol='responses' if native or '/responses' in path.path else 'anthropic' if path.path.endswith('/messages') else 'chat'
        if provider:
            mapping=json.loads((state.directory/'hub-connections.json').read_text(encoding='utf-8'))
            agent=mapping[provider['id']]['agent']
            token=self.headers.get('x-api-key') or self.headers.get('Authorization','').removeprefix('Bearer ')
            if not any(secrets.compare_digest(token,allowed) for allowed in ('agent-hub-'+agent,'magpie-'+agent,provider['key'])):
                return self.reply(401,{'error':{'message':'invalid local agent token'}})
            upstream_model=model if wb_agent else model[len(provider['id'])+1:]
            if upstream_model not in provider['models']:
                return self.reply(400,{'error':{'message':'model not configured on this connection'}})
            if provider.get('kind')=='workbuddy-ai':
                if self.command!='POST' or path.path.endswith('/compact'):
                    return self.reply(400,{'error':{'message':'WorkBuddy AI 不支持此端点；请发送完整消息进行推理'}})
                return self.forward_workbuddy(body,agent,provider['id'],model,upstream_model,protocol)
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

    def forward_workbuddy(self, body, agent, provider, model, upstream_model, protocol):
        from workbuddy import Accounts, ENDPOINT, WorkBuddyError, headers_for
        from workbuddy_protocol import Reply, conversation_headers, encode_event, sse_events, to_chat
        state=self.server.state
        start=time.monotonic();sid=session_id(self.headers,body)
        row={'engine':'agent-hub','schema':1,'t':now(),'agent':agent,'session':sid,
             'provider':provider,'req':model,'model':model,'route_id':uuid.uuid4().hex,'tries':1}
        with state.lock:
            generation=state.generation;capture_on=state.recording;state.active+=1
        observer=Observation(protocol);parts=context_parts(body)
        sent=False;conn=None;captured=bytearray();sensitive=[];reply=None
        done=threading.Event();cancelled=threading.Event()
        socket_holder=[]
        streaming=body.get('stream',False) is True
        scope=(provider,sid)
        try:
            auth=Accounts(state.directory).fresh()
            sensitive=[auth.get('access',''),auth.get('refresh','')]
            scope=(provider,sid,auth.get('uid'),auth.get('enterpriseId'),hashlib.sha256(auth.get('account','').encode()).hexdigest())
            previous=None
            previous_id=body.get('previous_response_id')
            if previous_id:
                if protocol!='responses': raise ValueError('previous_response_id 仅用于 Responses')
                with state.lock:
                    cached=state.workbuddy_history.get(previous_id)
                    if not cached or cached['scope']!=scope or cached['expires']<time.time():
                        raise ValueError('前序响应不可用，请在新会话中重发完整历史')
                    previous=cached['messages']
            chat,names=to_chat(dict(body,model=upstream_model),protocol,previous)
            headers=headers_for(auth)
            headers.update(conversation_headers(chat,sid,state.workbuddy_salt))
            url=urllib.parse.urlsplit(ENDPOINT)
            conn=(http.client.HTTPSConnection if url.scheme=='https' else http.client.HTTPConnection)(url.hostname,url.port,timeout=180)
            import queue
            incoming=queue.Queue(maxsize=16)
            def publish(kind,value=None):
                while not done.is_set():
                    try:incoming.put((kind,value),timeout=.1);return
                    except queue.Full:pass
            def read_upstream():
                try:
                    conn.connect()
                    socket_holder.append(conn.sock)
                    if done.is_set() or cancelled.is_set():return
                    conn.request('POST','/v2/chat/completions',packed(chat),headers)
                    response=conn.getresponse()
                    code=None
                    if response.status>=400:
                        raw=response.read(1024*1024)
                        try:code=json.loads(raw).get('code')
                        except (ValueError,AttributeError):pass
                    publish('headers',(response.status,response.getheader('Content-Type',''),code))
                    if response.status<400:
                        for chunk in sse_events(response):
                            if done.is_set():break
                            publish('chunk',chunk)
                    publish('done')
                except Exception as exc:publish('error',exc)
                finally:conn.close()
            def watch_client():
                import select
                import socket
                while not done.wait(.1):
                    try:
                        readable,_,_=select.select([self.connection],[],[],0)
                        if readable and not self.connection.recv(1,socket.MSG_PEEK):
                            cancelled.set()
                            for sock in socket_holder:
                                try:sock.shutdown(socket.SHUT_RDWR)
                                except OSError:pass
                            break
                    except OSError:
                        # Windows reports a peer closing with unread bytes as
                        # WSAECONNRESET rather than a readable zero-byte recv.
                        cancelled.set()
                        for sock in socket_holder:
                            try:sock.shutdown(socket.SHUT_RDWR)
                            except OSError:pass
                        break
            threading.Thread(target=watch_client,daemon=True).start()
            threading.Thread(target=read_upstream,daemon=True).start()
            def next_upstream():
                # Winsock shutdown can leave a synchronous recv waiting. Keep
                # the request handler interruptible even in that case; the
                # network worker owns and closes its upstream socket.
                while True:
                    if cancelled.is_set():raise WorkBuddyError('客户端已取消 WorkBuddy AI 请求',status=499)
                    try:kind,value=incoming.get(timeout=.1)
                    except queue.Empty:continue
                    if kind=='error':raise value
                    return kind,value
            kind,meta=next_upstream()
            if kind!='headers':raise WorkBuddyError('WorkBuddy AI 未返回响应头')
            status,content_type,code=meta
            row['status']=status
            if status>=400:
                # Preserve status and a safe vendor code, never raw auth-bearing text.
                row['err']='WorkBuddy AI HTTP '+str(status)+(f'（code {code}）' if isinstance(code,(int,float)) else '')
                if code==11140:row['err']+='；账号受限，请在 WorkBuddy 中提交反馈'
                self.reply(status,{'error':{'message':row['err'],'type':'upstream_error'}})
                return
            if 'text/event-stream' not in content_type.lower():
                raise WorkBuddyError('WorkBuddy AI 未返回流式响应')
            reply=Reply(protocol,upstream_model,names,[t['name'] for t in body.get('tools',[]) if t.get('type')=='custom'])
            observer.sse=streaming
            def send_events(values):
                nonlocal sent
                if not streaming:return
                if not sent:
                    self.send_response(200);self.send_header('Content-Type','text/event-stream; charset=utf-8')
                    self.send_header('Cache-Control','no-cache');self.send_header('Connection','close')
                    self.end_headers();self.close_connection=True;sent=True
                for value in values:
                    wire=encode_event(protocol,value)
                    self.wfile.write(wire);self.wfile.flush()
                    observer.feed(wire,round((time.monotonic()-start)*1000,2))
                    if capture_on:captured.extend(wire[:max(0,MAX_CAPTURE-len(captured))])
            while True:
                kind,chunk=next_upstream()
                if kind=='done':break
                if kind=='chunk':send_events(reply.feed(chunk))
            ending=reply.end()
            if protocol=='responses':
                messages=chat['messages']+[reply.assistant()]
                size=len(packed(messages))
                with state.lock:
                    state.workbuddy_history={k:v for k,v in state.workbuddy_history.items() if v['expires']>time.time()}
                    while state.workbuddy_history and (len(state.workbuddy_history)>=64 or sum(v['size'] for v in state.workbuddy_history.values())+size>MAX_BODY):
                        state.workbuddy_history.pop(next(iter(state.workbuddy_history)))
                    if size<=MAX_BODY:state.workbuddy_history[reply.id]={'scope':scope,'messages':messages,'size':size,'expires':time.time()+900}
            send_events(ending)
            if streaming:
                if protocol=='chat':self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush();observer.feed(b'data: [DONE]\n\n',round((time.monotonic()-start)*1000,2))
            else:
                result=reply.complete();wire=packed(result)
                self.reply(200,result);sent=True;observer.feed(wire,round((time.monotonic()-start)*1000,2))
                if capture_on:captured.extend(wire[:MAX_CAPTURE])
            observer.finish(round((time.monotonic()-start)*1000,2))
            row['served']=reply.model
        except (BrokenPipeError,ConnectionResetError):
            row.update(status=499,stop='aborted',err='客户端连接中断；已取消 WorkBuddy AI 请求')
        except (ValueError,OSError,http.client.HTTPException,TypeError,KeyError,AttributeError) as exc:
            status=499 if cancelled.is_set() else exc.status if isinstance(exc,WorkBuddyError) else 400 if isinstance(exc,ValueError) and reply is None and conn is None else 502
            if isinstance(exc,(TypeError,KeyError,AttributeError)) and conn is None:status=400
            message=str(exc) if isinstance(exc,ValueError) else 'WorkBuddy AI 请求格式无效' if status==400 else 'WorkBuddy AI 上游连接或响应读取失败'
            row.update(status=status,stop='aborted' if status==499 else 'error',err=message)
            try:
                if not sent and status!=499:self.reply(status,{'error':{'message':message}})
                elif sent and streaming and status!=499:
                    typ='error' if protocol=='anthropic' else 'response.failed' if protocol=='responses' else None
                    value={'type':typ,'error':{'type':'upstream_error','message':message}} if typ else {'error':{'message':message}}
                    if protocol=='responses':value={'type':'response.failed','response':{**reply.base('failed'),'error':{'code':'upstream_error','message':message}}}
                    self.wfile.write(encode_event(protocol,value));self.wfile.flush()
            except OSError:pass
        finally:
            done.set()
            if row.get('status',200)>=400:
                import socket
                for sock in socket_holder:
                    try:sock.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
            row={**observer.fields,**row};row['ms']=round((time.monotonic()-start)*1000,2)
            if reply:
                if 'prompt_tokens' not in reply.usage:
                    for key in ('in','cache_read','cache_write'):row.pop(key,None)
                if 'completion_tokens' not in reply.usage:row.pop('out',None)
                if 'reasoning_tokens' not in (reply.usage.get('completion_tokens_details') or {}):row.pop('reasoning',None)
            capture={'input':body,'output':captured.decode('utf-8',errors='replace'),'cut':len(captured)>=MAX_CAPTURE} if capture_on and sid else None
            try:state.save(row,parts,capture,generation,sensitive)
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
            cls=http.client.HTTPSConnection if url.scheme=='https' else http.client.HTTPConnection
            conn=cls(url.hostname,url.port,timeout=180)
            conn.request(self.command,urllib.parse.urlunsplit(('', '',url.path,url.query,'')),body=wire if self.command=='POST' else None,headers=headers)
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
            row.update(status=502,stop='error',err='上游连接、TLS 或响应读取失败（'+type(exc).__name__+'）')
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
                try:state.save(row,parts,capture,generation,sensitive)
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
    args=parser.parse_args()
    state=State(args.directory,os.environ.pop('AGENT_HUB_GATEWAY_CONTROL_KEY'))
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
