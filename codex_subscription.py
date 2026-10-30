"""Use an existing Codex ChatGPT login as a local, explicitly enabled provider.

The Codex auth file remains the only credential store. This module reads the
current access token for each operation and never refreshes or writes it.
"""
from __future__ import annotations

import base64
import copy
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import time
import urllib.parse

from agent_hub.gateway.transport import ProxyError, connection_for

PROVIDER = 'codex-chatgpt'
BACKEND = 'https://chatgpt.com/backend-api/codex'


class CodexSubscriptionError(ValueError):
    pass


def stream_error(event):
    error=event.get('error') or (event.get('response') or {}).get('error') or {}
    code=error.get('code') if isinstance(error,dict) else None
    if code in ('usage_limit_reached','insufficient_quota','subscription_sharing_usage_limit_exceeded','rate_limit_exceeded'):
        return 'Codex 订阅额度或速率受限，请查看账号用量后重试'
    if code in ('context_length_exceeded','context_window_exceeded'):
        return 'Codex 会话上下文过长；请缩短历史或新建会话'
    if code in ('invalid_api_key','token_expired','invalid_authentication'):
        return 'Codex 登录已失效，请在 Codex 重新登录'
    return 'Codex 订阅上游返回失败，请检查订阅和网络'


def stream_status(event):
    error=event.get('error') or (event.get('response') or {}).get('error') or {}
    code=error.get('code') if isinstance(error,dict) else None
    if code in ('usage_limit_reached','insufficient_quota','subscription_sharing_usage_limit_exceeded','rate_limit_exceeded'):return 429
    if code in ('context_length_exceeded','context_window_exceeded'):return 400
    if code in ('invalid_api_key','token_expired','invalid_authentication'):return 401
    return 502


def codex_home():
    from hub import HOME
    return Path(os.environ.get('CODEX_HOME') or HOME / '.codex').expanduser()


def auth_path():
    return codex_home() / 'auth.json'


def _expires(access):
    try:
        part = access.split('.')[1]
        data = json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
        exp = data.get('exp')
        return exp if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None
    except (IndexError, ValueError, TypeError):
        return None


def read_auth(path=None, *, allow_expired=False):
    path = Path(path or auth_path())
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError()
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        raise CodexSubscriptionError('无法读取 Codex 登录文件；请先在 Codex 登录 ChatGPT') from None
    if not isinstance(data, dict) or data.get('auth_mode') not in (None, 'chatgpt'):
        raise CodexSubscriptionError('当前 Codex 登录不是 ChatGPT 订阅')
    tokens = data.get('tokens')
    if not isinstance(tokens, dict) or not isinstance(tokens.get('access_token'), str) or not tokens['access_token']:
        raise CodexSubscriptionError('Codex 没有可用的 ChatGPT 登录；请先在 Codex 登录')
    account = tokens.get('account_id')
    if account is not None and (not isinstance(account, str) or len(account) > 512):
        raise CodexSubscriptionError('Codex 账号标识无效，请在 Codex 重新登录')
    expiry = _expires(tokens['access_token'])
    if not allow_expired and expiry is not None and expiry <= time.time():
        raise CodexSubscriptionError('Codex 登录已过期；请先打开 Codex 续期或重新登录')
    return {'access': tokens['access_token'], 'account': account or '', 'expires': expiry}


def status(path=None):
    try:
        auth = read_auth(path, allow_expired=True)
    except CodexSubscriptionError as exc:
        return {'signedIn': False, 'expired': False, 'message': str(exc)}
    expired = auth['expires'] is not None and auth['expires'] <= time.time()
    return {'signedIn': True, 'expired': expired,
            'message': 'Codex 登录已过期，请先在 Codex 续期' if expired else '使用 Codex 当前 ChatGPT 登录'}


def client_version(home=None):
    cache = Path(home or codex_home()) / 'models_cache.json'
    try:
        value = json.loads(cache.read_text(encoding='utf-8')).get('client_version')
        if isinstance(value, str) and re.fullmatch(r'\d+\.\d+\.\d+(?:[-.][A-Za-z0-9.-]+)?', value):
            return value
    except (OSError, ValueError, UnicodeError, AttributeError):
        pass
    raise CodexSubscriptionError('未找到 Codex 的模型目录版本；请先打开一次已登录的 Codex')


def headers(auth, version):
    result = {'Authorization': 'Bearer ' + auth['access'],
              'User-Agent': 'codex_cli_rs/' + version, 'Originator': 'codex_cli_rs'}
    if auth.get('account'):
        result['ChatGPT-Account-Id'] = auth['account']
    return result


def models(auth, home=None, *, backend=None):
    version = client_version(home)
    backend = backend or BACKEND
    url = backend + '/models?' + urllib.parse.urlencode({'client_version': version})
    conn = None
    try:
        conn, target, proxy_headers = connection_for(url, timeout=30)
        conn.request('GET', target, headers={**headers(auth, version), 'Accept': 'application/json', **proxy_headers})
        response = conn.getresponse()
        payload = response.read(8 * 1024 * 1024)
        if response.status != 200:
            raise CodexSubscriptionError(f'Codex 模型目录读取失败（HTTP {response.status}）；请检查登录与网络')
        data = json.loads(payload)
    except ProxyError as exc:
        raise CodexSubscriptionError(str(exc)) from None
    except CodexSubscriptionError:
        raise
    except (OSError, http.client.HTTPException, ValueError, TypeError, AttributeError):
        raise CodexSubscriptionError('Codex 模型目录不可用，请检查网络后重试') from None
    finally:
        if conn is not None:
            conn.close()
    if not isinstance(data, dict) or not isinstance(data.get('models'), list):
        raise CodexSubscriptionError('Codex 模型目录格式无效')
    result, seen = [], set()
    for item in data['models']:
        if not isinstance(item, dict) or item.get('visibility', 'list') != 'list':
            continue
        mid = item.get('slug') or item.get('id')
        if not isinstance(mid, str) or not mid.strip() or mid in seen:
            continue
        seen.add(mid)
        model = {'id': mid, 'name': str(item.get('display_name') or mid)}
        window = item.get('context_window')
        if type(window) is int and window > 0:
            model['contextWindow'] = window
        output = item.get('max_output_tokens')
        if type(output) is int and output > 0:
            model['maxTokens'] = output
        modalities = item.get('input_modalities')
        if isinstance(modalities, list) and modalities and all(isinstance(x, str) and x for x in modalities):
            model['input'] = list(dict.fromkeys(modalities))
        if item.get('supported_reasoning_levels'):
            model['reasoning'] = True
        result.append(model)
    if not result:
        raise CodexSubscriptionError('Codex 当前账号没有返回可选择的模型')
    return result


def provider_config(cfg, models, existing=None):
    from gateway_runtime import gateway_url
    p = copy.deepcopy(existing or {})
    existing_agents = p.get('agents') if isinstance(p.get('agents'), dict) else {}
    targets = p.get('targets') if isinstance(p.get('targets'), list) else ['claude', 'codex', 'pi', 'dsh', 'opencode']
    p.update(name='Codex ChatGPT', displayName='Codex／ChatGPT 订阅', kind=PROVIDER,
             baseUrl=gateway_url(cfg)+'/codex-subscription/pi/v1', api='openai-completions',
             apiKey=p.get('apiKey') or secrets.token_urlsafe(32), enabled=True,
             targets=targets, models=models)
    p['agents'] = {**existing_agents, **{agent: {**(existing_agents.get(agent) if isinstance(existing_agents.get(agent),dict) else {}),
                           'baseUrl': gateway_url(cfg)+'/codex-subscription/'+agent+('' if agent=='claude' else '/v1'),
                           'api': api} for agent, api in [('claude','anthropic-messages'), ('codex','openai-responses'),
                                                         ('pi','openai-completions'), ('dsh','openai-completions'),
                                                         ('opencode','openai-completions')]}}
    p['notes'] = '使用 Codex 当前 ChatGPT 登录；Hub 只读取 access token，不复制或刷新登录。使用时请保持 Hub 运行。'
    return p


def _response_content(value, role):
    if value is None:
        return []
    if isinstance(value, str):
        return [{'type': 'output_text' if role == 'assistant' else 'input_text', 'text': value}] if value else []
    if not isinstance(value, list):
        raise ValueError('不支持的消息内容')
    result = []
    for part in value:
        if not isinstance(part, dict):
            raise ValueError('不支持的消息内容')
        typ = part.get('type')
        if typ == 'text':
            result.append({'type': 'output_text' if role == 'assistant' else 'input_text', 'text': part.get('text', '')})
        elif typ == 'image_url' and role != 'assistant':
            value = part.get('image_url')
            image = value.get('url') if isinstance(value, dict) else value
            if not isinstance(image, str) or not image:
                raise ValueError('图片地址无效')
            result.append({'type': 'input_image', 'image_url': image})
        else:
            raise ValueError('Codex 订阅不支持该消息内容类型：' + str(typ))
    return result


def request_body(body, protocol, model):
    """Convert client Chat/Messages to Codex Responses; Responses stay native."""
    from agent_hub.gateway.protocol import ToolNames, to_chat
    if body.get('background'):
        raise ValueError('Codex 订阅不支持后台请求')
    if body.get('previous_response_id'):
        raise ValueError('Codex 订阅需要完整消息历史，暂不支持 previous_response_id')
    if protocol == 'responses':
        if 'stream' in body and type(body['stream']) is not bool:
            raise ValueError('stream 必须为布尔值')
        request = copy.deepcopy(body)
        request.update(model=model, stream=True, store=False)
        request['instructions'] = request.get('instructions') or 'You are a helpful assistant.'
        if isinstance(request.get('input'), str):
            request['input'] = [{'role':'user','content':[{'type':'input_text','text':request['input']}]}]
        request.pop('max_output_tokens', None)
        return request, ToolNames()
    # An opaque prior thought cannot be portably reconstructed across providers.
    if protocol == 'anthropic':
        for message in body.get('messages') or []:
            for part in message.get('content') if isinstance(message.get('content'), list) else []:
                if part.get('type') in ('thinking', 'redacted_thinking', 'encrypted_content'):
                    raise ValueError('无法迁移其他供应商的思考或加密内容；请从新会话开始')
    chat, names = to_chat(dict(body, model=model), protocol, normalize_client_metadata=False, preserve_tool_parts=True)
    instructions, items = [], []
    for message in chat['messages']:
        role = message.get('role')
        if role == 'system':
            parts = _response_content(message.get('content', ''), 'user')
            if any(p['type'] != 'input_text' for p in parts):
                raise ValueError('系统指令不支持图片')
            instructions.extend(p['text'] for p in parts if p.get('text'))
        elif role == 'tool':
            value = message.get('content', '')
            if isinstance(value,list):value=_response_content(value,'user')
            elif not isinstance(value,str):raise ValueError('工具结果须为文本或图片')
            items.append({'type': 'function_call_output', 'call_id': message.get('tool_call_id', ''), 'output': value})
        elif role in ('user', 'assistant'):
            parts = _response_content(message.get('content', ''), role)
            if parts:
                items.append({'role': role, 'content': parts})
            for call in message.get('tool_calls') or []:
                fn = call.get('function') or {}
                items.append({'type': 'function_call', 'call_id': call.get('id', ''),
                              'name': fn.get('name', ''), 'arguments': fn.get('arguments', '{}')})
        else:
            raise ValueError('Codex 订阅不支持消息角色：' + str(role))
    if not items:
        raise ValueError('请提供至少一条用户消息')
    request = {'model': model, 'instructions': '\n\n'.join(instructions) or 'You are a helpful assistant.',
               'input': items, 'store': False, 'stream': True}
    tools = []
    for tool in chat.get('tools') or []:
        fn = tool.get('function') or {}
        tools.append({'type': 'function', 'name': fn['name'], 'description': fn.get('description', ''),
                      'parameters': fn.get('parameters', {}), 'strict': False})
    if tools:
        request['tools'] = tools
    choice = chat.get('tool_choice')
    if choice:
        request['tool_choice'] = choice if choice in ('auto', 'required', 'none') else {'type': 'function', 'name': choice}
    if 'parallel_tool_calls' in chat:
        request['parallel_tool_calls'] = bool(chat['parallel_tool_calls'])
    if chat.get('reasoning_effort'):
        request['reasoning'] = {'effort': chat['reasoning_effort']}
    return request, names


class ResponsesToChat:
    """Translate ordered Responses SSE events to Chat chunks without replaying deltas."""
    def __init__(self):
        self.tools = {}
        self.text_seen = set()
        self.finished = False

    def _tool(self, index, item):
        state = self.tools.setdefault(index, {'name': '', 'id': '', 'arguments': '', 'opened': False})
        state['name'] = item.get('name') or state['name']
        state['id'] = item.get('call_id') or state['id']
        chunks = []
        if not state['opened'] and state['name'] and state['id']:
            state['opened'] = True
            chunks.append({'choices':[{'index':0, 'delta':{'tool_calls':[{'index':index, 'id':state['id'],
                'type':'function', 'function':{'name':state['name'], 'arguments':''}}]}}]})
        return state, chunks

    def feed(self, event):
        typ = event.get('type')
        index = event.get('output_index', 0)
        if typ == 'response.output_text.delta':
            self.text_seen.add(index)
            return [{'choices':[{'index':0, 'delta':{'content':event.get('delta', '')}}]}]
        if typ == 'response.refusal.delta':
            self.text_seen.add(index)
            return [{'choices':[{'index':0, 'delta':{'content':event.get('delta', '')}}]}]
        if typ == 'response.output_text.done' and index not in self.text_seen and event.get('text'):
            self.text_seen.add(index)
            return [{'choices':[{'index':0, 'delta':{'content':event['text']}}]}]
        if typ == 'response.reasoning_summary_text.delta':
            return [{'choices':[{'index':0, 'delta':{'reasoning_content':event.get('delta', '')}}]}]
        if typ == 'response.output_item.added' and (event.get('item') or {}).get('type') == 'function_call':
            _, chunks = self._tool(index, event['item'])
            return chunks
        if typ == 'response.function_call_arguments.delta':
            state = self.tools.get(index)
            if not state or not state['opened']:
                raise ValueError('Codex 工具参数早于工具身份，无法安全转换')
            part = event.get('delta', '')
            if not isinstance(part, str): raise ValueError('Codex 工具参数无效')
            state['arguments'] += part
            return [{'choices':[{'index':0, 'delta':{'tool_calls':[{'index':index,'function':{'arguments':part}}]}}]}]
        if typ == 'response.output_item.done':
            item = event.get('item') or {}
            if item.get('type') == 'function_call':
                state, chunks = self._tool(index, item)
                final = item.get('arguments', '')
                if not state['opened'] or not isinstance(final, str) or not final.startswith(state['arguments']):
                    raise ValueError('Codex 工具身份或参数不完整')
                rest = final[len(state['arguments']):]
                if rest:
                    state['arguments'] += rest
                    chunks.append({'choices':[{'index':0,'delta':{'tool_calls':[{'index':index,'function':{'arguments':rest}}]}}]})
                return chunks
            if item.get('type') == 'message' and index not in self.text_seen:
                text = ''.join(p.get('text', '') if p.get('type') == 'output_text' else p.get('refusal', '')
                               for p in item.get('content') or [] if p.get('type') in ('output_text','refusal'))
                if text:
                    self.text_seen.add(index)
                    return [{'choices':[{'index':0,'delta':{'content':text}}]}]
        if typ in ('response.failed', 'error'):
            raise ValueError(stream_error(event))
        if typ in ('response.completed', 'response.incomplete'):
            response = event.get('response') or {}
            if typ == 'response.incomplete' and (response.get('incomplete_details') or {}).get('reason') != 'max_output_tokens':
                raise ValueError('Codex 订阅上游响应未完成')
            self.finished = True
            pending = []
            for i, item in enumerate(response.get('output') or []):
                pending.extend(self.feed({'type':'response.output_item.done','output_index':i,'item':item}))
            usage = response.get('usage') or {}
            converted = {}
            for source, target in [('input_tokens','prompt_tokens'),('output_tokens','completion_tokens'),('total_tokens','total_tokens')]:
                if source in usage: converted[target] = usage[source]
            if isinstance(usage.get('input_tokens_details'), dict): converted['prompt_tokens_details'] = usage['input_tokens_details']
            if isinstance(usage.get('output_tokens_details'), dict): converted['completion_tokens_details'] = usage['output_tokens_details']
            result = {'model': response.get('model'), 'choices':[{'index':0,'delta':{},
                      'finish_reason':'length' if typ=='response.incomplete' else 'tool_calls' if self.tools else 'stop'}]}
            if converted: result['usage'] = converted
            return pending + [result]
        return []
