"""Opt-in real client probes, with disposable credentials/config and a harmless Read.

Usage: python scripts/verify-live-clients.py --agent claude --agent codex --agent pi
These calls use the account/route already configured locally and may incur usage.
No credential is printed or exported. Only scrubbed native logs and JSON events
are retained in the git-ignored tests/artifacts/live-clients directory.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import queue
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from observation import redact, native_files, parse_native

REAL = Path.home()
FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
WATCH = ['.claude/settings.json', '.claude.json', '.claude/.credentials.json',
         '.codex/config.toml', '.codex/auth.json', '.pi/agent/settings.json',
         '.pi/agent/auth.json', '.pi/agent/models.json', '.dsh/.credentials.yaml',
         '.dsh/settings.yaml', '.dsh/settings.yaml.imported', '.dsh/profiles/desktop/cordis.patch.yml']


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def fingerprints():
    return {p: hashlib.sha256((REAL / p).read_bytes()).hexdigest() if (REAL / p).exists() else None for p in WATCH}


def oauth_refresh_risk(credential, timeout):
    """Do not rotate a live account's refresh token inside a disposable copy."""
    if not isinstance(credential,dict) or credential.get('type')!='oauth':return False
    expiry=credential.get('expires')
    if not isinstance(expiry,(int,float)):return True
    if expiry>10**11:expiry/=1000
    return expiry<=time.time()+timeout+60


def leaves(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from leaves(v)
    elif isinstance(value, list):
        for v in value:
            yield from leaves(v)
    elif isinstance(value, str) and len(value) >= 12:
        yield value


def credential_values(value):
    if isinstance(value, dict):
        for name, v in value.items():
            normalized = name.lower().replace('_','').replace('-','')
            if (normalized in ('apikey','token','password','secret','authorization') or
                normalized.endswith(('apikey','accesstoken','refreshtoken','idtoken','authtoken'))):
                yield from leaves(v)
            elif isinstance(v, (dict, list)):
                yield from credential_values(v)
    elif isinstance(value, list):
        for v in value:
            yield from credential_values(v)


def scrub(value, secrets):
    # redact() limits a UI field to 16K; evidence files must retain full JSON lines.
    for secret in sorted(set(secrets), key=len, reverse=True):
        if len(secret) >= 12:
            value = value.replace(secret, '[REDACTED]')
    import re
    value = re.sub(r'\b(?:sk-[\w-]{8,}|gh[pousr]_[\w]{15,})', '[REDACTED]', value)
    value = re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[REDACTED]', value)
    value = re.sub(r'(https?://[^\s"<>?\\]+)\?[^\s"<>\\]*', r'\1?[REDACTED-QUERY]', value)
    return value


@contextmanager
def private_home(output, cleanup):
    scratch = Path(tempfile.mkdtemp(prefix='.private-', dir=output)).resolve()
    assert scratch.is_relative_to(output.resolve()) and scratch.name.startswith('.private-')
    try:
        yield str(scratch)
    finally:
        # Erase copied application credentials even when native Windows sandbox
        # ACLs prevent removing its administrator-owned sandbox_users.json.
        for relative in ('.codex/auth.json','.codex/config.toml','.codex/.agent-hub-state.json','.claude/settings.json','.claude/.credentials.json',
                         '.pi/agent/auth.json','.pi/agent/models.json','.dsh/.credentials.yaml',
                         '.dsh/settings.yaml','.dsh/settings.yaml.imported',
                         '.dsh/profiles/headless/cordis.patch.yml','.dsh/profiles/desktop/cordis.patch.yml'):
            (scratch/relative).unlink(missing_ok=True)
        protected = scratch/'.codex/.sandbox-secrets/sandbox_users.json'
        if protected.is_file():
            protected.write_bytes(b'')
        shutil.rmtree(scratch, ignore_errors=True)
        cleanup.update(credentialsRemoved=True,
                       remainingFiles=[{'path':str(p.relative_to(scratch)), 'bytes':p.stat().st_size}
                                       for p in scratch.rglob('*') if p.is_file()])


def capture(args, env, cwd, timeout, secrets, output, rpc_prompt=None, stream_only=False, rpc_model='gpt-6.1-sol', rpc_provider='openai'):
    started = time.time()
    proc = subprocess.Popen([str(x) for x in args], env=env, cwd=cwd,
                            stdin=subprocess.PIPE if rpc_prompt else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=FLAGS)
    streams = {'stdout': [], 'stderr': []}
    notifications = queue.Queue()
    def pump(name, pipe):
        for line in iter(pipe.readline, b''):
            streams[name].append({'elapsedMs': round((time.time()-started)*1000),
                                  'line': scrub(line.decode('utf-8', 'replace').rstrip(), secrets)})
            if name == 'stdout' and rpc_prompt:
                try:
                    notifications.put(json.loads(streams[name][-1]['line']))
                except ValueError:
                    pass
        pipe.close()
    readers = [threading.Thread(target=pump, args=(n, p), daemon=True) for n, p in [('stdout', proc.stdout), ('stderr', proc.stderr)]]
    for t in readers:
        t.start()
    timed_out = False
    harness_error = None
    try:
        if rpc_prompt:
            def send(value):
                proc.stdin.write((json.dumps(value)+'\n').encode())
                proc.stdin.flush()
            send({'id':1,'method':'initialize','params':{'clientInfo':{'name':'agent-hub-live-verify','version':'1.0'},'capabilities':{'experimentalApi':True}}})
            def start_thread():
                send({'id':2,'method':'thread/start','params':{'cwd':str(cwd),'model':rpc_model,
                      'modelProvider':rpc_provider,'approvalPolicy':'never','sandbox':'read-only'}})
            while time.time()-started < timeout:
                try:
                    event = notifications.get(timeout=1)
                except queue.Empty:
                    if proc.poll() is not None:
                        break
                    continue
                if event.get('id') == 1 and 'result' in event:
                    send({'method':'initialized','params':{}})
                    start_thread()
                elif event.get('method') == 'windowsSandbox/setupCompleted' and (event.get('params') or {}).get('success'):
                    start_thread()
                elif event.get('method') == 'windowsSandbox/setupCompleted':
                    break
                elif event.get('id') == 2 and 'result' in event:
                    send({'id':3,'method':'turn/start','params':{'threadId':event['result']['thread']['id'],
                          'input':[{'type':'text','text':rpc_prompt}], 'effort':'low'}})
                elif event.get('method') == 'turn/completed' or event.get('error') and 'id' in event:
                    break
                elif 'id' in event and event.get('method'):
                    # A probe never grants unexpected approval or credential requests.
                    send({'id':event['id'],'error':{'code':-32601,'message':'Unsupported in isolated read-only verification'}})
            else:
                raise subprocess.TimeoutExpired(args, timeout)
            proc.stdin.close()
            proc.wait(timeout=15)
        else:
            proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    except Exception as exc:
        harness_error = {'type':type(exc).__name__, 'message':scrub(str(exc),secrets)}
    finally:
        if proc.poll() is None:
            # Only the child process tree started above; never match/kill by application name.
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True, creationflags=FLAGS)
            proc.wait(timeout=15)
        if rpc_prompt and not proc.stdin.closed:
            proc.stdin.close()
        for t in readers:
            t.join(timeout=10)
    for name, lines in streams.items():
        (output / f'{name}.jsonl').write_text(''.join(json.dumps(x, ensure_ascii=False)+'\n' for x in lines), encoding='utf-8')
    events = []
    for line in streams['stdout']:
        try:
            events.append(json.loads(line['line']))
        except ValueError:
            pass
    return {'exitCode': proc.returncode, 'timedOut': timed_out,
            'harnessError': harness_error,
            'elapsedMs': round((time.time()-started)*1000), 'stdoutLines': len(streams['stdout']),
            'stderrLines': len(streams['stderr'])}, events, streams


def prepare_workbuddy(home, agent, model):
    """Use production adapters only inside the disposable HOME. No OAuth copies."""
    import copy
    from unittest.mock import patch
    import hub as engine
    cfg=read(ROOT/'providers.json')
    source=cfg.get('providers',{}).get('workbuddy-ai') or {}
    if source.get('kind')!='workbuddy-ai' or not source.get('enabled',True):raise ValueError('WorkBuddy AI is not connected')
    provider=copy.deepcopy(source)
    provider['models']=[m for m in source.get('models',[]) if m.get('id')==model]
    if not provider['models']:raise ValueError('Selected model is not in the WorkBuddy account catalog')
    config={'providers':{'workbuddy-ai':provider},'modelSync':{'default':{'provider':'workbuddy-ai','model':model}},
            'sync':{'targets':{agent:{'enabled':True,'mode':'third-party'}}}}
    if agent=='codex':
        (home/'.codex').mkdir(parents=True,exist_ok=True)
        (home/'.codex/config.toml').write_text('model = "original-placeholder"\n',encoding='utf-8')
    if agent=='dsh':
        (home/'.dsh/profiles/desktop').mkdir(parents=True)
        (home/'.dsh/profiles/desktop/cordis.patch.yml').write_text('[]\n',encoding='utf-8')
        (home/'.dsh/.credentials.yaml').write_text('version: 1\nrefs: {}\nrecords: {}\n',encoding='utf-8')
        (home/'.dsh/settings.yaml.imported').write_text('{}\n',encoding='utf-8')
    h=object.__new__(engine.Hub);h.cfg=config;h.providers=config['providers'];h.state={}
    def expand(path):
        value=str(path)
        return home/value[2:] if value.startswith('~/') else Path(value)
    with patch.multiple(engine,HOME=home,HUB_STATE=home/'.hub-state.json',HUB_BACKUP=home/'.hub-backups',expand=expand):
        getattr(h,'push_'+agent)()
    return provider


def probe(agent, scenario, timeout, out, transport='exec', stream_only=False, gateway=False, gateway_content=False, workbuddy_model=None):
    label = agent + ('-' + scenario if scenario != 'current' else '') + ('-app-server' if transport=='app-server' and agent=='codex' else '')
    output = out / label
    output.mkdir(parents=True)
    before = fingerprints()
    secrets = [v for k, v in os.environ.items() if any(s in k.upper() for s in ('TOKEN', 'API_KEY', 'SECRET', 'PASSWORD'))]
    # Codex deliberately refuses helper aliases under the OS temporary directory.
    # A disposable, git-ignored workspace directory preserves normal sandbox startup.
    cleanup = {}
    with private_home(output, cleanup) as scratch:
        home = Path(scratch)
        # mkdtemp's 0700 HOME protects credential copies. The Windows restricted
        # token cannot traverse it; the harmless task lives outside that HOME.
        cwd = output / 'probe-project'
        cwd.mkdir()
        nonce = 'HUB_READ_' + uuid.uuid4().hex[:12]
        (cwd / 'probe.txt').write_text(nonce+'\n', encoding='utf-8')
        prompt = ('This is an isolated read-only client verification. Use your file reading tool to read '
                  'probe.txt in the current working directory. Do not guess its content. Do not read any '
                  'other files, use subagents, network tools, or modify anything. Then respond with the '
                  'exact file content and the short sentence: Streaming and tool verification completed.')
        if stream_only:
            prompt = f'Do not use any tools. Reply with exactly this text: {nonce}. Streaming verification completed successfully.'
        env = {k:v for k,v in os.environ.items() if not any(s in k.upper() for s in
               ('TOKEN', 'API_KEY', 'SECRET', 'PASSWORD', 'CODEX_', 'CLAUDE_CONFIG_DIR', 'DSH_HOME', 'PI_CODING_AGENT'))}
        env.update(HOME=scratch, USERPROFILE=scratch, CLAUDE_CONFIG_DIR=str(home/'.claude'),
                   CODEX_HOME=str(home/'.codex'), PI_CODING_AGENT_DIR=str(home/'.pi/agent'),
                   DSH_HOME=str(home/'.dsh'), PI_TELEMETRY='0', PI_OFFLINE='1', NO_COLOR='1')
        env['APPDATA']=str(home/'AppData/Roaming')
        # Keep APPDATA/LOCALAPPDATA for Windows executable/runtime resolution; each
        # client is explicitly pointed at its private config root above.
        def clone(relative):
            src, dst = REAL / relative, home / relative
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                try:
                    secrets.extend(leaves(read(dst)) if 'auth' in src.name or 'credentials' in src.name else credential_values(read(dst)))
                except (ValueError, UnicodeError):
                    pass
            return dst
        node = shutil.which('node')
        npm = Path(os.environ['APPDATA']) / 'npm/node_modules'
        auth = {}
        skip_reason=None
        wb=prepare_workbuddy(home,agent,workbuddy_model) if workbuddy_model else None
        if wb:secrets.append(wb['apiKey'])
        if agent == 'claude':
            settings = read((home if wb else REAL)/'.claude/settings.json')
            route = settings.get('env', {})
            secrets.extend(v for k,v in route.items() if any(s in k.upper() for s in ('TOKEN','KEY','SECRET')))
            minimal = {'env': {k:v for k,v in route.items() if k in ('ANTHROPIC_AUTH_TOKEN','ANTHROPIC_API_KEY','ANTHROPIC_BASE_URL','ANTHROPIC_MODEL')},
                       'model': settings.get('model'), 'effortLevel':'low'}
            if scenario == 'native':
                minimal['env'] = {}
            write(home/'.claude/settings.json', minimal)
            if wb:
                # --bare deliberately doesn't apply settings.env. Supply this
                # isolated route explicitly, as required by Claude's CLI help.
                env.update(minimal['env'])
                env['ANTHROPIC_API_KEY']=wb['apiKey']
            if not wb:clone('.claude/.credentials.json')
            auth = {'nativeOAuthCachePresent': (home/'.claude/.credentials.json').is_file(),
                    'configuredApiCredentialPresent': any(minimal['env'].get(k) for k in ('ANTHROPIC_AUTH_TOKEN','ANTHROPIC_API_KEY'))}
            args = [REAL/'.local/bin/claude.exe', '--bare', '--settings', home/'.claude/settings.json',
                    '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                    '--tools', 'Read', '--allowedTools', 'Read', '--permission-prompts', 'none',
                    '--verbose', '--output-format', 'stream-json', '--include-partial-messages',
                    '--system-prompt', 'Read the requested file with Read, then answer briefly. Do not access other files.',
                    '--max-budget-usd', '0.50', '-p', prompt]
        elif agent == 'codex':
            if not wb:
                clone('.codex/auth.json')
                clone('.codex/models_cache.json')
            auth = {'authMode':'workbuddy-local-key'} if wb else {'authMode': read(home/'.codex/auth.json').get('auth_mode')}
            args = [node, npm/'@openai/codex/bin/codex.js', 'exec', '--json', '--ignore-user-config',
                    '--ignore-rules', '--skip-git-repo-check', '--sandbox', 'read-only',
                    '-c', 'cli_auth_credentials_store="file"', '-c', 'model_reasoning_effort="low"',
                    '-c', 'windows.sandbox="unelevated"',
                    '-m', 'gpt-6.1-sol', '-C', cwd, prompt]
            if transport == 'app-server':
                args = [node, npm/'@openai/codex/bin/codex.js', 'app-server', '--listen', 'stdio://',
                        '-c', 'cli_auth_credentials_store="file"', '-c', 'windows.sandbox="unelevated"']
            if wb and transport!='app-server':
                args.remove('--ignore-user-config');args[args.index('-m')+1]=workbuddy_model
            if wb:args[3:3]=['-c','web_search="disabled"']
        elif agent == 'pi':
            if wb:write(home/'.pi/agent/auth.json',{})
            else:clone('.pi/agent/auth.json')
            # API routes are copied only for the current-route probe. OAuth probe
            # uses the built-in provider, not a same-named third-party connection.
            settings = read((home if wb else REAL)/'.pi/agent/settings.json')
            if scenario == 'native':
                provider, model = 'openai', 'gpt-6.1-sol'
            else:
                if not wb:clone('.pi/agent/models.json')
                provider, model = settings['defaultProvider'], settings['defaultModel']
            auth = {'mode': 'oauth' if scenario == 'native' else read(home/'.pi/agent/auth.json').get(provider,{}).get('type'), 'provider':provider}
            if scenario=='native' and oauth_refresh_risk(read(home/'.pi/agent/auth.json').get(provider),timeout):
                skip_reason='隔离凭据的 OAuth 即将/已经过期；自动刷新可能轮换真实账号的 refresh token，跳过调用。请先在原生客户端完成登录。'
            write(home/'.pi/agent/settings.json', {'defaultProvider':provider, 'defaultModel':model, 'defaultThinkingLevel':'low'})
            package = npm/'@earendil-works/pi-coding-agent'
            entry = read(package/'package.json')['bin']
            entry = entry if isinstance(entry,str) else entry['pi']
            args = [node, package/entry, '--mode', 'json', '--print', '--provider', provider, '--model', model,
                    '--thinking', 'low', '--tools', 'read', '--no-extensions', '--no-mcp', '--no-skills',
                    '--no-prompt-templates', '--no-context-files', '--no-themes', '--no-approve', '--offline', prompt]
            if wb:args[args.index('--thinking')+1]='off'
        else:
            import yaml
            if not wb:clone('.dsh/.credentials.yaml')
            secrets.extend(leaves(yaml.safe_load((home/'.dsh/.credentials.yaml').read_text(encoding='utf-8'))))
            for name in ('settings.yaml','settings.yaml.imported'):
                if not wb:clone('.dsh/'+name)
                if (home/'.dsh'/name).exists():
                    secrets.extend(credential_values(yaml.safe_load((home/'.dsh'/name).read_text(encoding='utf-8'))))
            profile = home/'.dsh/profiles/headless'
            profile.mkdir(parents=True)
            for name in ('package.json','cordis.yml'):
                shutil.copy2(REAL/'.dsh/profiles/headless'/name, profile/name)
            desktop = yaml.safe_load(((home if wb else REAL)/'.dsh/profiles/desktop/cordis.patch.yml').read_text(encoding='utf-8'))
            patch = [x for x in desktop if x.get('id') in ('agent-default-model','llm-pi-ai')]
            if wb:patch.append({'id':'credentials','name':'@deepseek-ai/dsh-credentials-local','disabled':False,
                                'config':{'path':str(home/'.dsh/.credentials.yaml'),'watch':False}})
            if scenario == 'native':
                patch = [{'id':'agent-default-model','config':{'provider':'deepseek-account','model':'deepseek-flash','reasoningEffort':'high'}}]
            (profile/'cordis.patch.yml').write_text(yaml.safe_dump(patch,allow_unicode=True),encoding='utf-8')
            root = Path(os.environ['LOCALAPPDATA'])/'Programs/DeepSeek Harness'
            env['ELECTRON_RUN_AS_NODE']='1'
            args = [root/'DeepSeek Harness.exe','--expose-internals',root/'resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js',
                    '--profile','headless','--json',prompt]
            auth = {'mode':'native-account' if scenario == 'native' else 'configured-api-route'}
        runtime = None
        gateway_evidence = {'enabled':False}
        if gateway and not (agent in ('dsh','claude','pi') and scenario=='native'):
            from gateway_runtime import Runtime, route_id
            import socket
            with socket.socket() as port_socket:
                port_socket.bind(('127.0.0.1',0))
                port = port_socket.getsockname()[1]
            gw_cfg={'gateway':{'enabled':True,'port':port},'providers':{}}
            gw_url=f'http://127.0.0.1:{port}'
            if agent=='codex':
                # Preserve the auth.json mode; only inference uses this URL.
                args[3:3] = ['-c',f'openai_base_url="{gw_url}/backend-api/codex"']
            elif agent=='pi' and scenario=='native':
                write(home/'.pi/agent/models.json', {'providers':{'openai':{
                    'baseUrl':gw_url+'/backend-api/codex', 'headers':{'User-Agent':'pi/1.3.0'}}}})
            else:
                pid='probe'
                rid=route_id(agent,pid)
                if agent=='claude':
                    base=route['ANTHROPIC_BASE_URL'];secret=route.get('ANTHROPIC_AUTH_TOKEN') or route.get('ANTHROPIC_API_KEY')
                    model=route['ANTHROPIC_MODEL']; api='anthropic-messages'
                    minimal['env'].update(ANTHROPIC_BASE_URL=gw_url,ANTHROPIC_AUTH_TOKEN='agent-hub-claude',ANTHROPIC_MODEL=rid+'/'+model)
                    minimal.pop('model',None)
                    write(home/'.claude/settings.json',minimal)
                elif agent=='pi':
                    models=read(home/'.pi/agent/models.json');block=models['providers'][provider]
                    base=block['baseUrl'];api=block['api']
                    credential=read(home/'.pi/agent/auth.json').get(provider,{})
                    secret=block.get('apiKey') or credential.get('key')
                    if secret in os.environ:secret=os.environ[secret]
                    secrets.extend([secret] if secret else [])
                    block.update(baseUrl=gw_url+'/v1',apiKey='agent-hub-pi',compat={**block.get('compat',{}),'sendSessionAffinityHeaders':True})
                    for m in block.get('models',[]):m['id']=rid+'/'+m['id']
                    write(home/'.pi/agent/models.json',models)
                    args[args.index('--model')+1]=rid+'/'+model
                else:
                    selected=next(x['config'] for x in patch if x['id']=='agent-default-model')
                    provider=selected['provider'];model=selected['model']
                    block=next(x['config']['providers'][provider] for x in patch if x['id']=='llm-pi-ai')
                    base=block['baseURL'];api=block.get('api','openai-completions')
                    creds=yaml.safe_load((home/'.dsh/.credentials.yaml').read_text(encoding='utf-8'))
                    secret=(creds.get('refs') or {}).get(block['apiKeyEnv']) or os.environ.get(block['apiKeyEnv'])
                    if not isinstance(secret,str):raise ValueError('dsh probe credential ref must be a string')
                    block.update(baseURL=gw_url+'/v1',apiKeyEnv='HUB_PROBE_GATEWAY')
                    env['HUB_PROBE_GATEWAY']='agent-hub-dsh'
                    creds.setdefault('refs',{})['HUB_PROBE_GATEWAY']='agent-hub-dsh'
                    (home/'.dsh/.credentials.yaml').write_text(yaml.safe_dump(creds),encoding='utf-8')
                    for m in block.get('models',[]):m['id']=rid+'/'+m['id']
                    selected['model']=rid+'/'+model
                    (profile/'cordis.patch.yml').write_text(yaml.safe_dump(patch,allow_unicode=True),encoding='utf-8')
                    for name in ('settings.yaml','settings.yaml.imported'):
                        file=home/'.dsh'/name
                        value=yaml.safe_load(file.read_text(encoding='utf-8')) or {}
                        value.setdefault('llm-pi-ai',{}).setdefault('providers',{})[provider]=block
                        file.write_text(yaml.safe_dump(value,allow_unicode=True),encoding='utf-8')
                gw_cfg['providers'][pid]={'name':'Probe route','baseUrl':base,'api':api,'apiKey':secret,
                    'models':[{'id':model}],'targets':[agent]}
            runtime=Runtime(home/'gateway',lambda:gw_cfg,binary_root=ROOT/'data/runtime',codex_cache=home/'.codex/models_cache.json')
            runtime.start(gateway_content)
            gateway_evidence={'enabled':True,'version':runtime.status()['version'],'url':gw_url}
        print(f'START {label}: isolated config, '+('text stream only' if stream_only else 'one file-read task'), flush=True)
        try:
            if skip_reason:
                result={'exitCode':None,'timedOut':False,'harnessError':skip_reason,'skipped':True,'elapsedMs':0}
                events,streams=[],[]
            else:
                result, events, streams = capture(args, env, cwd, timeout, secrets, output,
                                                 prompt if agent=='codex' and transport=='app-server' else None, stream_only,
                                                 rpc_model=workbuddy_model or 'gpt-6.1-sol',rpc_provider='agent_hub' if wb else 'openai')
        finally:
            if runtime:
                if gateway_content:
                    bodies=list((runtime.directory/'gateway-conversations').rglob('turns.jsonl'))
                    owned_pid=runtime.process.pid
                    runtime.configure(False)
                    gateway_evidence.update(contentEnabledForProbe=True,capturedBodyFiles=len(bodies),
                        bodyFilesAfterDisable=len(list((runtime.directory/'gateway-conversations').rglob('turns.jsonl'))),
                        consentChangedWithoutProcessRestart=owned_pid==runtime.process.pid)
                runtime.close()
        if runtime:
            for route_path in (runtime.directory/'routing').glob('*.jsonl'):
                dest=output/'routing'/route_path.name
                dest.parent.mkdir(parents=True,exist_ok=True)
                dest.write_text(scrub(route_path.read_text(encoding='utf-8'),secrets),encoding='utf-8')
            ledger=runtime.directory/'usage.jsonl'
            if ledger.exists():
                rows=[json.loads(x) for x in ledger.read_text(encoding='utf-8').splitlines() if x]
                write(output/'gateway-usage.json',rows)
                (output/'gateway-usage.jsonl').write_text('\n'.join(scrub(json.dumps(x),secrets) for x in rows)+'\n',encoding='utf-8')
                gateway_evidence.update(records=len(rows),agents=sorted(set(r['agent'] for r in rows)),
                    responseIds=[r.get('response_id') for r in rows],sessionIds=sorted(set(r.get('session','') for r in rows)))
            else:gateway_evidence['records']=0
            write(output/'gateway-connections.json',json.loads((runtime.directory/'hub-connections.json').read_text(encoding='utf-8')))
        result['gateway']=gateway_evidence
        if wb:result['workbuddy']={'model':workbuddy_model,'provider':'workbuddy-ai','runningHubGateway':True,'oauthCopied':False}
        serialized = json.dumps(events, ensure_ascii=False)
        result.update(agent=agent, scenario=scenario, probeKind='stream' if stream_only else 'read', auth=auth, nonce=nonce,
                      nonceReturned=nonce in serialized, eventTypes=sorted(set(str(e.get('type') or e.get('method') or 'response') for e in events if isinstance(e,dict))))
        final_texts = []
        tool_results = []
        deltas = 0
        for e in events:
            t = e.get('type') or e.get('method')
            if agent == 'claude':
                if t == 'result' and not e.get('is_error'):
                    final_texts.append(e.get('result',''))
                if t == 'stream_event' and (e.get('event') or {}).get('type') == 'content_block_delta':
                    deltas += 1
                if t == 'user':
                    tool_results.extend(x for x in (e.get('message') or {}).get('content',[]) if isinstance(x,dict) and x.get('type')=='tool_result' and not x.get('is_error'))
            elif agent == 'pi':
                if t == 'message_end' and (e.get('message') or {}).get('role') == 'assistant' and e['message'].get('stopReason') == 'stop':
                    final_texts.extend(x.get('text','') for x in e['message'].get('content',[]) if x.get('type')=='text')
                if t == 'message_update' and (e.get('assistantMessageEvent') or {}).get('type') == 'text_delta':
                    deltas += 1
                if t == 'tool_execution_end' and not e.get('isError'):
                    tool_results.append(e)
            elif agent == 'dsh':
                if t == 'final':
                    final_texts.append(e.get('text',''))
                if t == 'text':
                    deltas += 1
                if t == 'tool_result' and e.get('status') == 'completed':
                    tool_results.append(e)
            elif transport == 'app-server':
                item = (e.get('params') or {}).get('item') or {}
                if t == 'item/completed' and item.get('type') == 'agentMessage':
                    final_texts.append(item.get('text',''))
                if t == 'item/agentMessage/delta':
                    deltas += 1
                if t == 'item/completed' and item.get('type') == 'commandExecution' and item.get('exitCode') == 0:
                    tool_results.append(item)
            else:
                item = e.get('item') or {}
                if t == 'item.completed' and item.get('type') == 'agent_message':
                    final_texts.append(item.get('text',''))
                if t == 'item.completed' and item.get('type') == 'command_execution' and item.get('exit_code') == 0:
                    tool_results.append(item)
        result.update(finalNonceVerified=any(nonce in x for x in final_texts), textDeltaEvents=deltas,
                      successfulToolResults=len(tool_results), transport=transport if agent=='codex' else 'json-stream')
        sources, _ = native_files(home)
        result['nativeFiles'] = []
        for a, path in sources:
            if a != agent:
                continue
            from observation import open_lines
            dest = output / 'native' / path.relative_to(home)
            dest = dest.with_suffix('') if dest.suffix in ('.zstd','.zst') else dest
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(''.join(scrub(json.dumps(r,ensure_ascii=False),secrets)+'\n' for r in open_lines(path)), encoding='utf-8')
            parsed = parse_native(agent, dest, True)
            result['nativeFiles'].append({'path':str(dest.relative_to(out)), 'requests':len(parsed.requests),
                                          'messages':len(parsed.messages), 'sessionId':parsed.sid})
        result['originalFilesUnchanged'] = fingerprints() == before
    result['cleanup'] = cleanup
    result['verified'] = (result['finalNonceVerified'] and not result['timedOut'] and not result['harnessError'] and
                          (result['textDeltaEvents'] > 0 if stream_only else result['successfulToolResults'] > 0)
                          and result['originalFilesUnchanged'] and cleanup.get('credentialsRemoved')
                          and all(x['bytes']==0 for x in cleanup.get('remainingFiles',[])))
    write(output/'result.json', result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', action='append', required=True, choices=['claude','codex','pi','dsh'])
    parser.add_argument('--scenario', choices=['current','native'], default='current')
    parser.add_argument('--timeout', type=int, default=150)
    parser.add_argument('--codex-transport', choices=['exec','app-server'], default='exec')
    parser.add_argument('--stream-only', action='store_true', help='Codex app-server text deltas; CLI probe verifies its file tool separately')
    parser.add_argument('--gateway',action='store_true',help='Use the repository-owned Hub gateway; never change the real client config')
    parser.add_argument('--gateway-content',action='store_true',help='Opt in for this harmless probe, then disable and clear gateway body captures')
    parser.add_argument('--workbuddy-model',help='Use the logged-in WorkBuddy provider through the running Hub, with isolated native configs and no OAuth copies')
    opts = parser.parse_args()
    if opts.stream_only and (opts.agent != ['codex'] or opts.codex_transport != 'app-server'):
        parser.error('--stream-only requires --agent codex --codex-transport app-server')
    if opts.workbuddy_model and (opts.scenario!='current' or opts.gateway or opts.gateway_content):
        parser.error('--workbuddy-model uses the running Hub; do not combine with native scenario or --gateway options')
    out = ROOT/'tests/artifacts/live-clients'/time.strftime('%Y%m%d-%H%M%S')
    out.mkdir(parents=True)
    print(f'ARTIFACTS {out}', flush=True)
    results = [probe(agent, opts.scenario, opts.timeout, out, opts.codex_transport, opts.stream_only, opts.gateway, opts.gateway_content,opts.workbuddy_model) for agent in opts.agent]
    return 0 if all(r['verified'] for r in results) else 1


if __name__ == '__main__':
    sys.exit(main())
