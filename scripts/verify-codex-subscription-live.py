"""Explicit live probe for Codex subscription sharing; consumes account quota.

Only an explicitly selected agent runs. Client homes and gateway files are
temporary; the existing Codex auth file is read by the gateway and never copied
or modified. The prompt asks the client to read a single synthetic file.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import codex_subscription as subscription
import hub as engine
from gateway_runtime import export_connections
from gateway_server import Handler,Server,State,packed
from agent_hub.observability.store import redact


def main():
    parser=argparse.ArgumentParser(description='真实 Codex 订阅共享验证；会消耗少量账号额度')
    parser.add_argument('--agent',action='append',choices=['claude','codex','pi','dsh','opencode'],required=True)
    parser.add_argument('--model',required=True,help='当前账号模型目录中的模型 ID')
    parser.add_argument('--timeout',type=int,default=120)
    args=parser.parse_args()
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    auth=subscription.read_auth();auth_file=subscription.auth_path()
    original=hashlib.sha256(auth_file.read_bytes()).digest()
    npm=Path(os.environ.get('APPDATA',''))/'npm/node_modules'
    node=shutil.which('node')
    marker='HUB_'+secrets.token_hex(6)
    results=[]
    artifacts=ROOT/'tests/artifacts';artifacts.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='hub-codex-live-',ignore_cleanup_errors=True) as directory, \
         tempfile.TemporaryDirectory(prefix='codex-live-project-',dir=artifacts,ignore_cleanup_errors=True) as project_dir:
        root=Path(directory).resolve();project=Path(project_dir).resolve()
        if (not root.is_relative_to(Path(tempfile.gettempdir()).resolve()) or
                project.parent != artifacts.resolve() or not project.name.startswith('codex-live-project-')):
            raise RuntimeError('临时验证目录不在预期范围内')
        (project/'probe.txt').write_text(marker,encoding='utf-8')
        if os.name=='nt' and 'codex' in args.agent:
            # Only this disposable synthetic directory/file receive read access;
            # the Windows read-only sandbox otherwise cannot traverse them.
            for path, grant in [(project, '*S-1-1-0:(RX)'), (project/'probe.txt', '*S-1-1-0:(R)')]:
                subprocess.run(['icacls',str(path),'/grant',grant],check=True,capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
        srv=Server(('127.0.0.1',0),Handler)
        cfg={'gateway':{'enabled':False,'port':srv.server_port},'providers':{}}
        cfg['providers']['codex-chatgpt']=subscription.provider_config(cfg,[{'id':args.model,'name':args.model,
            'contextWindow':128000,'maxTokens':8192,'input':['text'],'reasoning':True}])
        providers,mapping=export_connections(cfg);data=root/'gateway';data.mkdir()
        (data/'providers.json').write_bytes(packed(providers));(data/'hub-connections.json').write_bytes(packed(mapping))
        srv.state=State(data,'fixture-admin');threading.Thread(target=srv.serve_forever,daemon=True).start()
        local_key=cfg['providers']['codex-chatgpt']['apiKey'];base=f'http://127.0.0.1:{srv.server_port}'
        inherited={k:v for k,v in os.environ.items() if not any(x in k.upper() for x in
                   ('TOKEN','API_KEY','SECRET','PASSWORD','CODEX_','CLAUDE_CONFIG_DIR','DSH_HOME','PI_CODING_AGENT'))}
        prompt=('Read probe.txt in the current directory using your read or command tool. '
                'Read no other file and run no other command. Reply with exactly the file content.')
        try:
            for agent in dict.fromkeys(args.agent):
                home=root/agent/'home';home.mkdir(parents=True)
                (home/'.codex').mkdir()
                env=dict(inherited,HOME=str(home),USERPROFILE=str(home),CODEX_HOME=str(home/'.codex'),
                    CLAUDE_CONFIG_DIR=str(home/'.claude'),PI_CODING_AGENT_DIR=str(home/'.pi/agent'),
                    DSH_HOME=str(home/'.dsh'),XDG_CONFIG_HOME=str(home/'.config'),
                    XDG_DATA_HOME=str(home/'.local/share'),XDG_CACHE_HOME=str(home/'.cache'),
                    APPDATA=str(home/'AppData/Roaming'),LOCALAPPDATA=str(home/'AppData/Local'),
                    PI_TELEMETRY='0',PI_OFFLINE='1',OPENCODE_DISABLE_AUTOUPDATE='1',NO_COLOR='1',
                    HUB_TEST_KEY=local_key,NO_PROXY='127.0.0.1,localhost')
                h=object.__new__(engine.Hub)
                h.cfg={**cfg,'modelSync':{'default':{'provider':'codex-chatgpt','model':args.model}},
                       'sync':{'targets':{agent:{'enabled':True,'mode':'third-party'}}}}
                h.providers=h.cfg['providers'];h.state={}
                def expand(path):
                    value=str(path);return home/value[2:] if value.startswith('~/') else Path(value)
                if agent=='dsh':
                    (home/'.dsh/profiles/desktop').mkdir(parents=True)
                    (home/'.dsh/profiles/desktop/cordis.patch.yml').write_text('[]\n',encoding='utf-8')
                    (home/'.dsh/.credentials.yaml').write_text('version: 1\nrefs: {}\nrecords: {}\n',encoding='utf-8')
                    (home/'.dsh/settings.yaml.imported').write_text('{}\n',encoding='utf-8')
                if agent in ('pi','dsh','opencode'):
                    with patch.multiple(engine,HOME=home,HUB_STATE=home/'.hub-state.json',
                                        HUB_BACKUP=home/'.hub-backups',expand=expand):
                        getattr(h,'push_'+agent)()
                if agent=='claude':
                    env.update(ANTHROPIC_BASE_URL=base+'/codex-subscription/claude',
                               ANTHROPIC_AUTH_TOKEN=local_key,ANTHROPIC_API_KEY=local_key,ANTHROPIC_MODEL=args.model)
                    command=[shutil.which('claude'),'--bare','--strict-mcp-config','--mcp-config','{"mcpServers":{}}',
                        '--tools','Read','--allowedTools','Read','--permission-prompts','none','--verbose',
                        '--output-format','stream-json','--include-partial-messages','--model',args.model,
                        '--system-prompt','Only use Read on probe.txt, then reply briefly.','-p',prompt]
                elif agent=='pi':
                    package=next((p for p in (npm/'@earendil-works/pi-coding-agent',npm/'@mariozechner/pi-coding-agent')
                                  if (p/'package.json').is_file()),None)
                    if not package or not node:raise RuntimeError('未找到 pi 的 npm 安装')
                    entry=json.loads((package/'package.json').read_text(encoding='utf-8')).get('bin')
                    pi_bin=package/(entry if isinstance(entry,str) else entry['pi'])
                    command=[node,str(pi_bin),'--mode','json','--print','--provider','codex-chatgpt',
                        '--model',args.model,'--thinking','off','--tools','read','--no-extensions','--no-mcp',
                        '--no-skills','--no-prompt-templates','--no-context-files','--no-themes','--no-approve','--offline',prompt]
                elif agent=='codex':
                    js=npm/'@openai/codex/bin/codex.js'
                    if not node or not js.is_file():raise RuntimeError('未找到 Codex 的 npm 安装')
                    command=[node,str(js),'exec','--json','--ignore-user-config','--ignore-rules','--skip-git-repo-check',
                        '--sandbox','read-only','-c','cli_auth_credentials_store="file"','-c','model_reasoning_effort="low"',
                        '-c','windows.sandbox="unelevated"','-c','model_provider="hub"','-c','model_providers.hub.name="Hub"',
                        '-c',f'model_providers.hub.base_url="{base}/codex-subscription/codex/v1"',
                        '-c','model_providers.hub.env_key="HUB_TEST_KEY"','-c','model_providers.hub.wire_api="responses"',
                        '-m',args.model,'-C',str(project),prompt]
                elif agent=='dsh':
                    import yaml
                    profile=home/'.dsh/profiles/headless';profile.mkdir(parents=True)
                    for filename in ('package.json','cordis.yml'):
                        shutil.copy2(Path.home()/'.dsh/profiles/headless'/filename,profile/filename)
                    desktop=yaml.safe_load((home/'.dsh/profiles/desktop/cordis.patch.yml').read_text(encoding='utf-8'))
                    entries=[x for x in desktop if x.get('id') in ('agent-default-model','llm-pi-ai')]
                    entries.append({'id':'credentials','name':'@deepseek-ai/dsh-credentials-local','disabled':False,
                                    'config':{'path':str(home/'.dsh/.credentials.yaml'),'watch':False}})
                    (profile/'cordis.patch.yml').write_text(yaml.safe_dump(entries,allow_unicode=True),encoding='utf-8')
                    root_cli=Path(os.environ.get('LOCALAPPDATA',''))/'Programs/DeepSeek Harness'
                    env['ELECTRON_RUN_AS_NODE']='1'
                    command=[str(root_cli/'DeepSeek Harness.exe'),'--expose-internals',
                        str(root_cli/'resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js'),
                        '--profile','headless','--json',prompt]
                else:
                    path=home/'.config/opencode/opencode.json'
                    config=json.loads(path.read_text(encoding='utf-8'));config['permission']={'*':'deny','read':'allow'}
                    path.write_text(json.dumps(config),encoding='utf-8');env['OPENCODE_CONFIG']=str(path)
                    command=[shutil.which('opencode'),'--print-logs','--log-level','debug','run','--standalone',
                             '--format','json','--model','codex-chatgpt/'+args.model,prompt]
                if not command[0]:raise RuntimeError(f'未找到 {agent} 可执行文件')
                try:
                    proc=subprocess.run(command,env=env,cwd=project,stdin=subprocess.DEVNULL,capture_output=True,text=True,encoding='utf-8',
                                        errors='replace',timeout=args.timeout,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                    events=[]
                    for line in proc.stdout.splitlines():
                        try:events.append(json.loads(line))
                        except ValueError:pass
                    texts=[]
                    for event in events:
                        item=event.get('item') or {};message=event.get('message') or {};part=event.get('part') or {}
                        item=item if isinstance(item,dict) else {}
                        message=message if isinstance(message,dict) else {}
                        part=part if isinstance(part,dict) else {}
                        if item.get('type')=='agent_message' and isinstance(item.get('text'),str):texts.append(item['text'])
                        if message.get('role')=='assistant':
                            texts.extend(p.get('text','') for p in message.get('content') or [] if isinstance(p,dict) and p.get('type')=='text')
                        if event.get('type')=='text' and isinstance(part.get('text'),str):texts.append(part['text'])
                        if event.get('type')=='result' and isinstance(event.get('result'),str):texts.append(event['result'])
                        if event.get('type')=='final' and isinstance(event.get('text'),str):texts.append(event['text'])
                    tool=(any((e.get('item') or {}).get('type')=='command_execution' and (e.get('item') or {}).get('exit_code')==0 for e in events)
                          if agent=='codex' else any(e.get('type')=='tool_result' and e.get('status')=='completed' for e in events)
                          if agent=='dsh' else 'tool' in proc.stdout.lower() and 'read' in proc.stdout.lower())
                    answered=any(marker in text for text in texts)
                    ok=proc.returncode==0 and tool and answered
                    results.append(ok)
                    print(f'{agent}: exit={proc.returncode} tool={tool} final_marker={answered}',flush=True)
                    if not ok:
                        safe=redact((proc.stderr+'\n'+proc.stdout)[-4000:],[local_key,auth['access'],auth['account']])
                        print('diagnostic:',safe[-1800:],flush=True)
                except subprocess.TimeoutExpired:
                    results.append(False);print(f'{agent}: timed out',flush=True)
            ledger=''.join(p.read_text(encoding='utf-8') for p in data.rglob('*.jsonl'))
            clean=not any(value and value in ledger for value in (auth['access'],auth['account']))
            unchanged=hashlib.sha256(auth_file.read_bytes()).digest()==original
            print(f'auth file unchanged={unchanged}; credentials absent from test ledger={clean}',flush=True)
            return 0 if all(results) and clean and unchanged else 1
        finally:
            srv.shutdown();srv.server_close()


if __name__=='__main__':sys.exit(main())
