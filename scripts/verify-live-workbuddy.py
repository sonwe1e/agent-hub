"""Opt-in real WorkBuddy probes. Uses the running Hub; never copies OAuth tokens.

Only synthetic prompts and an isolated random file are sent to the model.
Example: python scripts/verify-live-workbuddy.py --out tests/artifacts/workbuddy/live-20261019
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import http.client
import json
from pathlib import Path
import secrets
import sys
import time
import urllib.parse
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from observation import redact


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


class Probe:
    def __init__(self,out,model):
        self.out=out.resolve();self.out.mkdir(parents=True,exist_ok=False)
        cfg=json.loads((ROOT/'providers.json').read_text(encoding='utf-8'))
        provider=cfg['providers']['workbuddy-ai']
        if provider.get('kind')!='workbuddy-ai' or not provider.get('enabled',True):raise ValueError('WorkBuddy provider is not enabled')
        if model not in {m['id'] for m in provider['models']}:raise ValueError('Model not present in account catalog')
        self.key=provider['apiKey'];self.model=model
        self.base=f'http://127.0.0.1:{(cfg.get("gateway") or {}).get("port",8789)}'
        self.results=[]

    def ask(self,protocol,body,sid,label):
        agent={'chat':'pi','anthropic':'claude','responses':'codex'}[protocol]
        endpoint={'chat':'/v1/chat/completions','anthropic':'/v1/messages','responses':'/v1/responses'}[protocol]
        url=urllib.parse.urlsplit(self.base)
        conn=http.client.HTTPConnection(url.hostname,url.port,timeout=180)
        start=time.monotonic();first=None;events=[];pending=b'';lines=[];done=False
        try:
            conn.request('POST','/workbuddy/'+agent+endpoint,
                json.dumps(body,ensure_ascii=False).encode(),{'Content-Type':'application/json',
                'Authorization':'Bearer '+self.key,'X-Session-Id':sid})
            response=conn.getresponse()
            if response.status>=400:
                error=redact(response.read(65536).decode('utf-8','replace'),[self.key])
                result={'label':label,'protocol':protocol,'httpStatus':response.status,'error':error,
                        'elapsedMs':round((time.monotonic()-start)*1000)}
                self.results.append(result);write(self.out/(label+'.json'),result)
                raise ValueError(error)
            if not body.get('stream'):
                value=json.loads(response.read(16*1024*1024))
            else:
                while True:
                    chunk=response.read1(65536)
                    if not chunk:break
                    pending+=chunk
                    while b'\n' in pending:
                        line,pending=pending.split(b'\n',1);line=line.rstrip(b'\r')
                        if line.startswith(b'data:'):lines.append(line[5:].lstrip())
                        elif not line and lines:
                            data=b'\n'.join(lines);lines=[]
                            if data==b'[DONE]':done=True;continue
                            event=json.loads(data)
                            events.append(event)
                            typ=event.get('type','')
                            if first is None and (typ.endswith('.delta') or typ=='content_block_delta' or any((c.get('delta') or {}).get('content') or (c.get('delta') or {}).get('tool_calls') for c in event.get('choices',[]))):
                                first=round((time.monotonic()-start)*1000)
                if protocol=='chat':
                    message={'role':'assistant','content':''};calls={};usage={};finish=None
                    for event in events:
                        if event.get('error'):raise ValueError(str(event['error']))
                        if event.get('usage'):usage=event['usage']
                        for choice in (event.get('choices') or [])[:1]:
                            delta=choice.get('delta') or {};message['content']+=delta.get('content') or ''
                            for tool in delta.get('tool_calls') or []:
                                item=calls.setdefault(tool.get('index',0),{'id':'','type':'function','function':{'name':'','arguments':''}})
                                if tool.get('id'):item['id']=tool['id']
                                for k in ('name','arguments'):item['function'][k]+=(tool.get('function') or {}).get(k) or ''
                            finish=choice.get('finish_reason') or finish
                    if calls:message['tool_calls']=list(calls.values())
                    if not done or not finish:raise ValueError('Chat stream lacks completion marker')
                    value={'id':next((e['id'] for e in events if e.get('id')),None),'choices':[{'message':message,'finish_reason':finish}],'usage':usage}
                elif protocol=='anthropic':
                    value={};blocks={};arguments={}
                    for event in events:
                        typ=event.get('type')
                        if typ=='message_start':value=event['message']
                        elif typ=='content_block_start':blocks[event['index']]=event['content_block']
                        elif typ=='content_block_delta':
                            block=blocks[event['index']];delta=event['delta']
                            if delta['type']=='text_delta':block['text']+=delta['text']
                            elif delta['type']=='thinking_delta':block['thinking']+=delta['thinking']
                            elif delta['type']=='input_json_delta':arguments[event['index']]=arguments.get(event['index'],'')+delta['partial_json']
                        elif typ=='message_delta':value.update(event['delta']);value['usage']=event.get('usage') or value.get('usage')
                        elif typ=='error':raise ValueError(str(event.get('error')))
                    for i,args in arguments.items():blocks[i]['input']=json.loads(args)
                    value['content']=[blocks[i] for i in sorted(blocks)]
                    if not any(e.get('type')=='message_stop' for e in events):raise ValueError('Anthropic stream lacks message_stop')
                else:
                    value=next((e['response'] for e in events if e.get('type')=='response.completed'),None)
                    if not value:raise ValueError('Responses stream lacks response.completed: '+str([e for e in events if e.get('type') in ('error','response.failed','response.incomplete')]))
            result={'label':label,'protocol':protocol,'model':self.model,'httpStatus':response.status,
                    'responseId':value.get('id'),'firstDeltaMs':first,'elapsedMs':round((time.monotonic()-start)*1000),
                    'eventCount':len(events),'usage':value.get('usage'),'response':value,'events':events}
            self.results.append(result);write(self.out/(label+'.json'),result)
            print(json.dumps({k:result[k] for k in ('label','protocol','httpStatus','firstDeltaMs','elapsedMs','eventCount')},ensure_ascii=True),flush=True)
            return value
        finally:conn.close()

    def tools(self,protocol,custom=False):
        folder=self.out/protocol;folder.mkdir()
        token='WB_TOOL_'+secrets.token_hex(12);(folder/'probe.txt').write_text(token+'\n',encoding='utf-8')
        sid='workbuddy-live-'+protocol+'-'+uuid.uuid4().hex
        prompt='Use read_probe to read probe.txt. Do not guess its contents. After the tool result, reply with exactly the returned verification token and nothing else.'
        schema={'type':'object','properties':{'filename':{'type':'string','enum':['probe.txt']}},'required':['filename'],'additionalProperties':False}
        tool={'name':'read_probe','description':'Read the isolated verification file probe.txt; its contents are not in the prompt.'}
        common={'model':self.model,'stream':True,'max_tokens':1024}
        if protocol=='chat':
            body={**common,'messages':[{'role':'user','content':prompt}],'tools':[{'type':'function','function':{**tool,'parameters':schema}}],
                  'tool_choice':{'type':'function','function':{'name':'read_probe'}}}
        elif protocol=='anthropic':
            body={**common,'messages':[{'role':'user','content':prompt}],'tools':[{**tool,'input_schema':schema}],
                  'tool_choice':{'type':'tool','name':'read_probe'}}
        else:
            body={**common,'input':prompt,'tools':[{'type':'function',**tool,'parameters':schema}],
                  'tool_choice':{'type':'function','name':'read_probe'}}
            if custom:
                body['tools']=[{'type':'namespace','name':'functions','description':'Local verification tools',
                                'tools':[{'type':'custom',**tool,'format':{'type':'text'}}]}]
                body['tool_choice']={'type':'custom','namespace':'functions','name':'read_probe'}
                body['input']='Use read_probe with exactly probe.txt as its text input. After the tool result, reply with exactly the verification token it returned and nothing else.'
        prefix=protocol+('-custom' if custom else '')
        first=self.ask(protocol,body,sid,prefix+'-tool')
        if protocol=='chat':
            calls=first['choices'][0]['message'].get('tool_calls') or []
            if len(calls)!=1:raise ValueError('Expected one tool call')
            call=calls[0];name=call['function']['name'];args=json.loads(call['function']['arguments']);callid=call['id']
        else:
            calls=[v for v in first.get('content',first.get('output',[])) if v.get('type') in ('tool_use','function_call','custom_tool_call')]
            if len(calls)!=1:raise ValueError('Expected one tool call')
            call=calls[0];name=call['name'];args={'filename':call.get('input','').strip()} if custom else call.get('input') if protocol=='anthropic' else json.loads(call['arguments']);callid=call.get('call_id',call.get('id'))
            if custom and call.get('namespace')!='functions':raise ValueError('Custom tool namespace was not restored')
        if name!='read_probe' or args.get('filename')!='probe.txt':raise ValueError('Unexpected tool or filename; execution refused')
        actual=(folder/'probe.txt').read_text(encoding='utf-8').strip()
        body.pop('tool_choice',None)
        if protocol=='chat':body['messages'] += [first['choices'][0]['message'],{'role':'tool','tool_call_id':callid,'content':actual}]
        elif protocol=='anthropic':body['messages'] += [{'role':'assistant','content':first['content']},{'role':'user','content':[{'type':'tool_result','tool_use_id':callid,'content':actual}]}]
        else:
            body['previous_response_id']=first['id'];body['input']=[{'type':'custom_tool_call_output' if custom else 'function_call_output','call_id':callid,'output':actual}]
        last=self.ask(protocol,body,sid,prefix+'-result')
        answer=last['choices'][0]['message'].get('content','') if protocol=='chat' else '\n'.join(p.get('text','') for p in last['content']) if protocol=='anthropic' else '\n'.join(p.get('text','') for item in last['output'] if item.get('type')=='message' for p in item.get('content',[]))
        passed=answer.strip()==token
        report={'protocol':protocol,'verified':passed,'toolName':name,'callId':callid,'expected':token,'answer':answer,
                'toolExecuted':True,'sessionId':sid,'previousResponseContinuation':protocol=='responses','customTextTool':custom}
        write(self.out/(prefix+'-verification.json'),report)
        print(json.dumps(report,ensure_ascii=True),flush=True)
        return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--model',default='default-model')
    parser.add_argument('--protocol',choices=['chat','anthropic','responses'],action='append')
    parser.add_argument('--custom',action='store_true',help='Verify a namespaced Responses custom text tool')
    args=parser.parse_args();probe=Probe(args.out,args.model);checks=[]
    if args.custom and args.protocol!=['responses']:parser.error('--custom requires --protocol responses')
    for protocol in args.protocol or ('chat','anthropic','responses'):
        try:checks.append(probe.tools(protocol,args.custom))
        except Exception as exc:
            checks.append({'protocol':protocol,'verified':False,'error':redact(str(exc),[probe.key])})
            print(json.dumps(checks[-1],ensure_ascii=True),flush=True)
            if any(str(code) in str(exc) for code in ('11140','HTTP 401')):break
    summary={'at':dt.datetime.now(dt.timezone.utc).isoformat(),'model':args.model,'checks':checks,
             'requests':len(probe.results),'verified':all(c['verified'] for c in checks)}
    write(probe.out/'verification.json',summary)
    return 0 if summary['verified'] else 1


if __name__=='__main__':raise SystemExit(main())
