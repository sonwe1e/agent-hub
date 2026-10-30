"""Explicit live multi-turn subscription probe; consumes account quota.

Uses disposable local gateway and synthetic text/images. Native login files are
read by the gateway, never copied or changed. Only selected providers run.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import http.client
from io import BytesIO
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading

from PIL import Image
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import codex_subscription as codex
import zcode_subscription as zcode
from gateway_runtime import export_connections
from gateway_server import Handler,Server,State,packed


def main():
    p=argparse.ArgumentParser(description='真实订阅多轮、工具与图像验证；会消耗账号额度')
    p.add_argument('--provider',action='append',choices=['codex','zcode'],required=True)
    p.add_argument('--codex-model',default='gpt-6-luna');p.add_argument('--zcode-model',default='GLM-5.3-Flash')
    p.add_argument('--image-only',action='store_true',help='仅重新验证图片输入')
    args=p.parse_args()
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    os.environ['NO_PROXY']='127.0.0.1,localhost'
    config={'gateway':{'enabled':False},'providers':{}}
    auths={};files={}
    if 'codex' in args.provider:auths['codex']=codex.read_auth();files['codex']=codex.auth_path()
    if 'zcode' in args.provider:auths['zcode']=zcode.read_auth();files['zcode']=zcode.data_dir()/'credentials.json'
    before={k:hashlib.sha256(v.read_bytes()).digest() for k,v in files.items()}
    marker='HUB'+secrets.token_hex(5).upper();tool_marker='TOOL'+secrets.token_hex(5).upper()
    picture=BytesIO();Image.new('RGB',(256,256),(235,20,20)).save(picture,format='PNG')
    image64=base64.b64encode(picture.getvalue()).decode('ascii');dataurl='data:image/png;base64,'+image64
    ok=True
    with tempfile.TemporaryDirectory(prefix='agent-hub-conversation-probe-',ignore_cleanup_errors=True) as tmp:
        root=Path(tmp).resolve();assert root.is_relative_to(Path(tempfile.gettempdir()).resolve())
        srv=Server(('127.0.0.1',0),Handler);config['gateway']['port']=srv.server_port
        if 'codex' in args.provider:config['providers']['codex-chatgpt']=codex.provider_config(config,[{'id':args.codex_model}])
        if 'zcode' in args.provider:config['providers']['zcode-subscription']=zcode.provider_config(config,[{'id':args.zcode_model}],auth=auths['zcode'])
        providers,mapping=export_connections(config)
        (root/'providers.json').write_bytes(packed(providers));(root/'hub-connections.json').write_bytes(packed(mapping))
        srv.state=State(root,'synthetic-admin');threading.Thread(target=srv.serve_forever,daemon=True).start()
        def request(name,agent,protocol,body):
            ident='codex-chatgpt' if name=='codex' else 'zcode-subscription';prefix='codex-subscription' if name=='codex' else ident
            path={'chat':'/v1/chat/completions','anthropic':'/v1/messages','responses':'/v1/responses'}[protocol]
            connection=http.client.HTTPConnection('127.0.0.1',srv.server_port,timeout=120)
            try:
                connection.request('POST','/'+prefix+'/'+agent+path,packed(body),{'Content-Type':'application/json',
                    'Authorization':'Bearer '+config['providers'][ident]['apiKey']})
                response=connection.getresponse();value=json.loads(response.read());return response.status,value
            finally:connection.close()
        def text_of(value,protocol):
            if protocol=='chat':return value['choices'][0]['message'].get('content') or ''
            if protocol=='anthropic':return ''.join(x.get('text','') for x in value.get('content',[]) if x.get('type')=='text')
            return ''.join(x.get('text','') for item in value.get('output',[]) for x in item.get('content',[]) if x.get('type')=='output_text')
        try:
            for name in dict.fromkeys(args.provider):
                model=args.codex_model if name=='codex' else args.zcode_model
                for agent,protocol in [('pi','chat'),('claude','anthropic'),('codex','responses')]:
                    if args.image_only:break
                    base={'model':model,'stream':False}
                    if protocol=='anthropic':base['max_tokens']=256
                    first='Remember this code for a later question: '+marker+'. Reply only READY.'
                    if protocol=='responses':body={**base,'input':[{'role':'user','content':first}]}
                    else:body={**base,'messages':[{'role':'user','content':first}]}
                    try:
                        status,one=request(name,agent,protocol,body)
                        if status!=200:raise ValueError('first request HTTP '+str(status))
                        answer=text_of(one,protocol)
                        history=[{'role':'user','content':first},{'role':'assistant','content':answer}]
                        for i in range(5):history.extend([{'role':'user','content':f'Confirm step {i+1} briefly.'},{'role':'assistant','content':f'Step {i+1} confirmed.'}])
                        history.append({'role':'user','content':'What was the original code? Return only the code.'})
                        body={**base,('input' if protocol=='responses' else 'messages'):history}
                        status,two=request(name,agent,protocol,body)
                        history_ok=status==200 and marker in text_of(two,protocol)
                        schema={'type':'object','properties':{'key':{'type':'string'}},'required':['key']}
                        if protocol=='chat':
                            body={**base,'messages':[{'role':'user','content':'Call get_probe, then report its result.'}],
                                  'tools':[{'type':'function','function':{'name':'get_probe','description':'Read a test value','parameters':schema}}],
                                  'tool_choice':{'type':'function','function':{'name':'get_probe'}}}
                        elif protocol=='anthropic':
                            body={**base,'messages':[{'role':'user','content':'Call get_probe, then report its result.'}],
                                  'tools':[{'name':'get_probe','description':'Read a test value','input_schema':schema}],
                                  'tool_choice':{'type':'tool','name':'get_probe'}}
                        else:
                            body={**base,'input':[{'role':'user','content':'Call get_probe, then report its result.'}],
                                  'tools':[{'type':'function','name':'get_probe','description':'Read a test value','parameters':schema}],
                                  'tool_choice':{'type':'function','name':'get_probe'}}
                        status,tool=request(name,agent,protocol,body)
                        if status!=200:raise ValueError('tool request HTTP '+str(status))
                        if protocol=='chat':
                            assistant=tool['choices'][0]['message'];call=assistant['tool_calls'][0]
                            body['messages'] += [assistant,{'role':'tool','tool_call_id':call['id'],'content':tool_marker}]
                        elif protocol=='anthropic':
                            call=next(x for x in tool['content'] if x.get('type')=='tool_use')
                            body['messages'] += [{'role':'assistant','content':tool['content']},{'role':'user','content':[{'type':'tool_result','tool_use_id':call['id'],'content':tool_marker}]}]
                        else:
                            call=next(x for x in tool['output'] if x.get('type')=='function_call')
                            body['input'] += [call,{'type':'function_call_output','call_id':call['call_id'],'output':tool_marker}]
                        body.pop('tool_choice',None)
                        status,finished=request(name,agent,protocol,body)
                        tool_ok=status==200 and tool_marker in text_of(finished,protocol)
                        ok=ok and history_ok and tool_ok
                        print(name,agent,'history',history_ok,'tool_roundtrip',tool_ok,flush=True)
                    except (ValueError,KeyError,IndexError,StopIteration,TypeError,OSError) as exc:
                        ok=False;print(name,agent,'failed',type(exc).__name__,str(exc)[:180],flush=True)
                # Image goes through the Chat conversion; vendor model capability
                # comes from the account's current model directory.
                image_body={'model':model,'stream':False,'messages':[{'role':'user','content':[{'type':'text',
                    'text':'What is the solid image color? Reply exactly RED or BLUE.'},{'type':'image_url','image_url':{'url':dataurl}}]}]}
                try:
                    status,value=request(name,'pi','chat',image_body);reply=text_of(value,'chat') if status==200 else ''
                    image_ok=status==200 and ('RED' in reply.upper() or '红' in reply)
                    ok=ok and image_ok;print(name,'image_input',image_ok,'status',status,flush=True)
                    if not image_ok:print(name,'synthetic image answer',repr(reply[:180]),flush=True)
                except (ValueError,KeyError,TypeError,OSError) as exc:
                    ok=False;print(name,'image_input failed',type(exc).__name__,flush=True)
            ledger=''.join(f.read_text(encoding='utf-8') for f in root.rglob('*.jsonl'))
            sensitive=[v for a in auths.values() for v in (a.get('access'),a.get('account'),a.get('key'),a.get('token'),a.get('jwt')) if v]
            clean=not any(value in ledger for value in sensitive)
            unchanged=all(hashlib.sha256(path.read_bytes()).digest()==before[k] for k,path in files.items())
            print('auth files unchanged',unchanged,'credentials absent from ledger',clean,flush=True)
            ok=ok and clean and unchanged
        finally:srv.shutdown();srv.server_close()
    return 0 if ok else 1


if __name__=='__main__':sys.exit(main())
