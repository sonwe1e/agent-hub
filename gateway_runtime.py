"""Own the repository's Python gateway and compile reversible client routing.

The native adapters and sync_transaction remain the only writers of client files.
This component exports explicit Hub connections and owns only its child process.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import shutil
import secrets
import subprocess
import sys
import threading
import time
import urllib.request

VERSION = 'hub-2'
DEFAULT_PORT = 8789


def own_windows_process(process):
    """Close-on-parent-death job: a crashed Hub must not leave a gateway orphan."""
    if os.name!='nt':return None
    import ctypes
    from ctypes import wintypes as w
    class Limits(ctypes.Structure):
        _fields_=[('processTime',ctypes.c_longlong),('jobTime',ctypes.c_longlong),('flags',w.DWORD),
            ('minWS',ctypes.c_size_t),('maxWS',ctypes.c_size_t),('active',w.DWORD),
            ('affinity',ctypes.c_size_t),('priority',w.DWORD),('schedule',w.DWORD)]
    class Extended(ctypes.Structure):
        _fields_=[('basic',Limits),('io',ctypes.c_ulonglong*6),('processMemory',ctypes.c_size_t),
            ('jobMemory',ctypes.c_size_t),('peakProcess',ctypes.c_size_t),('peakJob',ctypes.c_size_t)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateJobObjectW.restype=w.HANDLE
    kernel.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
    kernel.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
    kernel.CloseHandle.argtypes=[w.HANDLE]
    handle=kernel.CreateJobObjectW(None,None)
    info=Extended();info.basic.flags=0x2000 # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not handle or not kernel.SetInformationJobObject(handle,9,ctypes.byref(info),ctypes.sizeof(info)) or not kernel.AssignProcessToJobObject(handle,w.HANDLE(int(process._handle))):
        if handle:kernel.CloseHandle(handle)
        raise ValueError('无法建立网关进程退出保护，已停止启动')
    return kernel,handle


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(value, ensure_ascii=False, indent=2)
    if path.exists() and path.read_text(encoding='utf-8') == rendered:
        return
    temp = path.with_suffix('.tmp')
    temp.write_text(rendered, encoding='utf-8')
    try:
        temp.chmod(0o600)
    except OSError:
        pass
    temp.replace(path)


def route_id(agent, provider):
    return 'hub-' + agent + '-' + hashlib.sha256(provider.encode()).hexdigest()[:12]


def gateway_url(cfg):
    block = cfg.get('gateway') or {}
    port = block.get('port', DEFAULT_PORT)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('网关端口必须为 1024–65535')
    return f'http://127.0.0.1:{port}'


def export_connections(cfg):
    from agent_inventory import AGENTS
    from hub import agent_connection, agent_connection_ready, provider_targets
    rows, mapping = [], {}
    for pid, provider in (cfg.get('providers') or {}).items():
        if provider.get('enabled', True) is False:
            continue
        for agent in AGENTS:
            if not provider_targets(provider, agent) or not agent_connection_ready(provider, agent):
                continue
            conn = agent_connection(provider, agent)
            secret = conn['apiKey'] or os.environ.get(conn.get('apiKeyEnv') or '', '')
            if not secret:
                continue
            protocol = {'anthropic-messages':'anthropic', 'openai-responses':'responses',
                        'openai-completions':'chat'}.get(conn['api'])
            if not protocol:
                continue
            rid = route_id(agent, pid)
            rows.append({'id':rid, 'name':provider.get('displayName') or provider.get('name') or pid,
                         'key':secret, protocol:conn['baseUrl'],
                         'models':[m['id'] for m in conn['models']], 'fallback':[]})
            if provider.get('kind') == 'workbuddy-ai':
                rows[-1]['kind'] = 'workbuddy-ai'
            mapping[rid] = {'agent':agent, 'provider':pid}
    return {'providers':rows}, mapping


def install_compiler_routes(module, h):
    """Wrap the existing adapters inside their write-confined compiler process.

    Hub selections retain their real IDs. Only rendered client model IDs receive
    Hub's explicit connection namespace, including connection-specific models.
    """
    if not (h.cfg.get('gateway') or {}).get('enabled'):
        return
    from copy import deepcopy
    url = gateway_url(h.cfg)
    original_connection, original_tiers = module.agent_connection, module.agent_model_tiers
    identities = {id(p):pid for pid,p in h.providers.items()}

    def connection(provider, target):
        result = deepcopy(original_connection(provider, target))
        agent = module.canonical_target(target)
        pid = identities.get(id(provider))
        # Some adapters use a shallow copy when filtering providers.
        if pid is None:
            pid = next((k for k,p in h.providers.items() if p == provider), None)
        if not pid or agent not in ('claude','codex','pi','dsh'):
            return result
        rid = route_id(agent,pid)
        result['baseUrl'] = url if agent=='claude' else url+'/v1'
        result['apiKey'], result['apiKeyEnv'] = 'agent-hub-'+agent, ''
        result['models'] = [dict(m,id=rid+'/'+m['id']) for m in result['models']]
        if agent == 'pi':
            result['compat'] = {**(result.get('compat') or {}), 'sendSessionAffinityHeaders':True}
        return result

    def tiers(cfg, target):
        result = original_tiers(cfg, target)
        agent = module.canonical_target(target)
        if agent in ('claude','codex','pi','dsh'):
            for row in result.values():
                prefix = route_id(agent,row['provider'])+'/'
                if not row['model'].startswith(prefix):
                    row['model'] = prefix+row['model']
        return result

    module.agent_connection, module.agent_model_tiers = connection, tiers


def prepare_compiler_native(module,h,targets):
    """Re-key retained per-model options before existing adapters reconcile them."""
    enabled=bool((h.cfg.get('gateway') or {}).get('enabled'))
    if 'pi' in targets:
        path=module.HOME/'.pi/agent/models.json'
        data=module.load_json(path) if path.exists() else None
        if isinstance(data,dict):
            for pid,block in (data.get('providers') or {}).items():
                if pid not in h.providers or not isinstance(block,dict):continue
                prefix=route_id('pi',pid)+'/'
                known={m['id'] for m in module.agent_connection(h.providers[pid],'pi')['models']}
                for model in block.get('models') or []:
                    mid=model.get('id','')
                    bare=mid[len(prefix):] if mid.startswith(prefix) else mid
                    if bare in known:model['id']=prefix+bare if enabled else bare
            module.save_json(path,data)
    if 'claude' in targets:
        path=module.HOME/'.claude/settings.json'
        data=module.load_json(path) if path.exists() else None
        if not isinstance(data,dict):return
        env=data.get('env') or {}
        main=module.agent_model_tiers(h.cfg,'claude').get('model')
        preserved=((h.cfg.get('modelSync') or {}).get('overrides',{}).get('claude') or {}).get('preserveTiers',[])
        for name,value in list(env.items()):
            if not isinstance(value,str) or not (name.startswith('ANTHROPIC_') and ('_MODEL' in name) or name=='CLAUDE_CODE_SUBAGENT_MODEL'):
                continue
            bare=value
            for pid in h.providers:
                prefix=route_id('claude',pid)+'/'
                if value.startswith(prefix):bare=value[len(prefix):];break
            if not enabled:env[name]=bare
            elif not name.endswith('_NAME') and main and (any(t.upper() in name for t in preserved) or 'subagent' in preserved and name=='CLAUDE_CODE_SUBAGENT_MODEL'):
                pid=main['provider'];known={m['id'] for m in module.agent_connection(h.providers[pid],'claude')['models']}
                if bare not in known:raise ValueError('保留的 Claude 档位不在选定连接模型库中；请先明确其模型，未改写配置')
                env[name]=route_id('claude',pid)+'/'+bare
        data['env']=env
        module.save_json(path,data)


def compile_codex_native(home, cfg):
    """Own only the native provider URL, with an independent conflict/restore record."""
    import tomllib
    from codex_config import _set_root
    home = Path(home)
    path = home/'.codex/config.toml'
    state_path = home/'.codex/.agent-hub-gateway.json'
    if not path.exists():
        return
    enabled = bool((cfg.get('gateway') or {}).get('enabled'))
    text = path.read_text(encoding='utf-8')
    data = tomllib.loads(text)
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else None
    current = data.get('openai_base_url')
    if state and current != state.get('written'):
        raise ValueError('Codex 网关地址已被外部修改；保留外部配置，请检查后重新预览')
    lines = text.splitlines()
    if enabled:
        desired = gateway_url(cfg)+'/backend-api/codex'
        if not state:
            state = {'present':'openai_base_url' in data, 'value':current}
        state['written']=desired
        _set_root(lines,'openai_base_url',desired)
        write_json(state_path,state)
    elif state:
        _set_root(lines,'openai_base_url',state.get('value'),state['present'])
        state_path.unlink()
    else:
        return
    path.write_text('\n'.join(lines)+'\n',encoding='utf-8')


class Runtime:
    def __init__(self, root, config=lambda: {}, binary_root=None, codex_cache=None):
        self.root = Path(root).absolute()
        self.config = config
        self.binary_root = Path(binary_root or self.root.parent/'runtime').absolute()
        self.binary = Path(__file__).with_name('gateway_server.py')
        self.codex_cache=Path(codex_cache) if codex_cache else None
        self.directory = self.root/'config/agent-hub'
        self.process = None
        self.lock = threading.RLock()
        self.error = None
        self.stage = 'stopped'
        self.log = None
        self.stop_event = threading.Event()
        self.monitor = None
        self.job = None
        self.admin_port = None
        self.admin_key = None
        self.recording = False
        self.log_thread = None

    def status(self):
        running = self.process is not None and self.process.poll() is None
        return {'state': 'running' if running else self.stage if self.stage in ('downloading','starting','failed') else 'stopped',
                'version':VERSION, 'url':gateway_url(self.config()), 'error':self.error,
                'pid':self.process.pid if running else None, 'installed':self.binary.exists(),
                'enabled':(self.config().get('gateway') or {}).get('activeEnabled',(self.config().get('gateway') or {}).get('enabled',False)),
                'pending': 'activeEnabled' in (self.config().get('gateway') or {}),
                'ledger':str(self.directory/'usage.jsonl'),
                'engine':'agent-hub',
                'coverage':'Hub 自有网关转发自定义线路、WorkBuddy AI 海外版与 Codex 原生登录；pi 内置 OAuth、dsh 自带账号保持原生直连。'}

    def install(self):
        if not self.binary.is_file():
            raise ValueError('缺少 Hub 网关源文件 gateway_server.py')

    def configure(self, content=False, cfg=None):
        # Only model metadata is available to an optional offline fallback.
        if self.codex_cache and self.codex_cache.is_file():
            try:
                cached=json.loads(self.codex_cache.read_text(encoding='utf-8'))
                if isinstance(cached.get('models'),list) and cached['models']:
                    write_json(self.root/'home/.codex/models_cache.json',cached)
            except (ValueError,OSError):
                pass
        providers, mapping = export_connections(cfg or self.config())
        write_json(self.directory/'providers.json', providers)
        write_json(self.directory/'hub-connections.json', mapping)
        running=self.process is not None and self.process.poll() is None
        write_json(self.directory/'settings.json', {
            'engine':'agent-hub','schema':1,'accountDiscovery':False,
            'gatewayConversations':self.recording if running else False,
            'requestArchive':False,'automaticRetries':0,
        })
        if running and content!=self.recording:
            self.set_recording(content)
        elif not running and not content:
            self.clear_content()
        return mapping

    def set_recording(self, on):
        # The child holds a lock/generation around consent and content writes.
        request=urllib.request.Request(f'http://127.0.0.1:{self.admin_port}/recording',
            data=json.dumps({'on':bool(on),'clear':not on}).encode(),method='POST',
            headers={'Content-Type':'application/json','Authorization':'Bearer '+self.admin_key})
        with urllib.request.urlopen(request,timeout=5) as response:
            if json.load(response).get('recording')!=bool(on):
                raise ValueError('网关正文采集开关未生效')
        self.recording=bool(on)
        settings=json.loads((self.directory/'settings.json').read_text(encoding='utf-8'))
        write_json(self.directory/'settings.json',{**settings,'gatewayConversations':self.recording})

    def clear_content(self):
        target=self.directory/'gateway-conversations'
        if target.exists():
            resolved=target.resolve()
            if resolved.parent != self.directory.resolve() or target.is_symlink():
                raise ValueError('网关正文目录路径异常，未清理')
            shutil.rmtree(target)

    def start(self, content=False):
        with self.lock:
            try:
                if self.process and self.process.poll() is None:
                    self.configure(content)
                    return self.status()
                self.install()
                self.configure(content)
                if self.job:
                    self.job[0].CloseHandle(self.job[1]);self.job=None
                self.stage, self.error = 'starting', None
                address = ('127.0.0.1', int(gateway_url(self.config()).rsplit(':',1)[1]))
                # Refuse an occupied port; never adopt or stop somebody else's gateway.
                with socket.socket() as check:
                    check.bind(address)
                env = {k:v for k,v in os.environ.items() if not k.startswith('MAGPIE_') and
                       not any(word in k.upper() for word in ('TOKEN','API_KEY','SECRET','PASSWORD','CODEX_'))}
                isolated = str(self.root/'home')
                Path(isolated).mkdir(parents=True, exist_ok=True)
                env.update(HOME=isolated, USERPROFILE=isolated, XDG_CONFIG_HOME=str(self.root/'config'),
                           XDG_CACHE_HOME=str(self.root/'cache'), XDG_DATA_HOME=str(self.root/'share'),
                           CODEX_HOME=str(self.root/'home/.codex'), CLAUDE_CONFIG_DIR=str(self.root/'home/.claude'),
                           PI_CODING_AGENT_DIR=str(self.root/'home/.pi/agent'), DSH_HOME=str(self.root/'home/.dsh'),
                           DO_NOT_TRACK='1', NO_COLOR='1')
                env.update(APPDATA=str(self.root/'home/AppData/Roaming'),LOCALAPPDATA=str(self.root/'home/AppData/Local'))
                with socket.socket() as admin_socket:
                    admin_socket.bind(('127.0.0.1',0));self.admin_port=admin_socket.getsockname()[1]
                self.admin_key=secrets.token_urlsafe(32)
                env['AGENT_HUB_GATEWAY_CONTROL_KEY']=self.admin_key
                if self.log:
                    self.log.close()
                self.log = (self.root/'gateway.log').open('ab')
                self.process = subprocess.Popen([sys.executable, str(self.binary),'--directory',str(self.directory),
                    '--port',str(address[1]),'--control-port',str(self.admin_port)], env=env, cwd=self.root,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                self.job=own_windows_process(self.process)
                sensitive=[self.admin_key]+[p['key'] for p in export_connections(self.config())[0]['providers']]
                def diagnostics(pipe, log):
                    from observation import redact
                    try:
                        for line in iter(pipe.readline,b''):
                            log.write((redact(line.decode('utf-8',errors='replace').rstrip(),sensitive)+'\n').encode('utf-8'))
                            log.flush()
                    finally:pipe.close()
                self.log_thread=threading.Thread(target=diagnostics,args=(self.process.stdout,self.log),daemon=True)
                self.log_thread.start()
                deadline = time.monotonic()+20
                while time.monotonic()<deadline:
                    if self.process.poll() is not None:
                        raise ValueError('网关退出；查看本机 gateway.log 诊断')
                    try:
                        with urllib.request.urlopen(gateway_url(self.config())+'/v1/models',timeout=.5) as response:
                            if response.status==200:
                                self.recording=False
                                self.set_recording(content)
                                self.stage='running'
                                return self.status()
                    except OSError:
                        time.sleep(.1)
                raise ValueError('网关启动超时')
            except Exception as exc:
                self.stop()
                self.stage, self.error = 'failed', str(exc) if isinstance(exc,ValueError) else type(exc).__name__
                raise ValueError(self.error) from None

    def stop(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                try:
                    request=urllib.request.Request(f'http://127.0.0.1:{self.admin_port}/drain',
                        data=b'{}',headers={'Authorization':'Bearer '+self.admin_key,'Content-Type':'application/json'},method='POST')
                    with urllib.request.urlopen(request,timeout=6) as response:response.read()
                except (OSError,TypeError):pass
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            self.process = None
            if self.job:
                self.job[0].CloseHandle(self.job[1])
                self.job=None
            if self.log:
                if self.log_thread:self.log_thread.join(timeout=2)
                self.log.close()
                self.log = None
            self.recording=False
            self.admin_key=None
            self.stage='stopped'
        return self.status()

    def close(self):
        self.stop_event.set()
        if self.monitor:
            self.monitor.join(timeout=2)
        self.stop()

    def watch(self, settings=lambda: {}):
        if self.monitor and self.monitor.is_alive():
            return
        def loop():
            while not self.stop_event.is_set():
                block=self.config().get('gateway') or {}
                needs_workbuddy=any(p.get('kind')=='workbuddy-ai' and p.get('enabled',True)
                                   for p in (self.config().get('providers') or {}).values())
                if block.get('activeEnabled',block.get('enabled',True)) or needs_workbuddy:
                    try:
                        self.start(settings().get('enabled',True) and settings().get('content',False))
                    except ValueError:
                        pass
                self.stop_event.wait(5)
        self.monitor=threading.Thread(target=loop,daemon=True)
        self.monitor.start()
