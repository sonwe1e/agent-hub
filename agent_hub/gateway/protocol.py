"""Shared request and stream conversion for the Codex and ZCode adapters.

Some conventions originate in the retired WorkBuddy adapter and reference
magpie-community/plugins (MIT); see docs/WORKBUDDY-LICENSE.txt.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
import time
import uuid


class ToolNames(dict):
    def __init__(self):
        super().__init__()
        self.identities={}
        self.custom=set()


def response_tools(tools):
    """Flatten Responses namespaces without losing callable identity."""
    result=[]
    for tool in tools:
        if tool.get('type')!='namespace':
            result.append(copy.deepcopy(tool));continue
        namespace=tool.get('name')
        if not isinstance(namespace,str) or not namespace:raise ValueError('工具命名空间不能为空')
        for child in tool.get('tools') or []:
            if child.get('type') not in ('function','custom'):raise ValueError('命名空间只能包含函数或自定义文本工具')
            child=copy.deepcopy(child)
            child['_identity']={'namespace':namespace,'name':child['name']}
            child['name']=namespace+'.'+child['name']
            if tool.get('description'):child['description']=tool['description']+'\n'+child.get('description','')
            result.append(child)
    return result


def client_metadata(messages):
    """Normalize client boilerplate for WorkBuddy; keep tool results verbatim.

    These exact compatibility spellings follow Magpie's WorkBuddy adapter.
    Anthropic's billing transport header is not an instruction for this vendor.
    """
    def normalize(value):
        value=re.sub(r'^x-anthropic-billing-header:[^\n]*(?:\n|$)','',value,flags=re.I|re.M)
        value=re.sub(r"You are Claude Code, Anthropic['’]s official CLI for Claude", "You are Claude Code, Anthropic's CLI for Claude",value,flags=re.I)
        value=re.sub(r'Main branch \(you will usually use this for PRs\)','Main branch (usually the base for PRs)',value,flags=re.I)
        return re.sub(r'(?<!the )Codex CLI is an open source project led by OpenAI\.', 'The Codex CLI is an open source project led by OpenAI.',value,flags=re.I)
    result=[]
    for message in messages:
        if message.get('role')=='tool':result.append(message);continue
        c=message.get('content')
        if isinstance(c,str):message['content']=normalize(c)
        elif isinstance(c,list):
            message['content']=[dict(p,text=normalize(p['text'])) if isinstance(p.get('text'),str) else p for p in c]
            message['content']=[p for p in message['content'] if p.get('type')!='text' or p.get('text','').strip()]
        if message.get('role')=='system' and not message.get('content'):continue
        result.append(message)
    return result


def text(value):
    if isinstance(value,str): return value
    if isinstance(value,list): return '\n'.join(p.get('text','') for p in value if isinstance(p,dict) and p.get('type') in ('text','input_text','output_text'))
    return json.dumps(value,ensure_ascii=False)


def content(value):
    if isinstance(value,str) or value is None: return value or ''
    result=[]
    for part in value:
        typ=part.get('type')
        if typ in ('text','input_text','output_text'): result.append({'type':'text','text':part.get('text','')})
        elif typ=='image_url': result.append(part)
        elif typ=='input_image': result.append({'type':'image_url','image_url':{'url':part['image_url']}})
        elif typ=='image':
            source=part.get('source') or {}
            url=source.get('url') if source.get('type')=='url' else 'data:'+source.get('media_type','image/png')+';base64,'+source.get('data','')
            result.append({'type':'image_url','image_url':{'url':url}})
        elif typ in ('thinking','redacted_thinking','tool_use','tool_result','encrypted_content'): continue
        else: raise ValueError('WorkBuddy AI 暂不支持此消息内容类型：'+str(typ))
    return result or ''


def _enum_type(value):
    if value is None: return 'null'
    if isinstance(value,bool): return 'boolean'
    if isinstance(value,int): return 'integer'
    if isinstance(value,float): return 'number'
    if isinstance(value,str): return 'string'
    if isinstance(value,dict): return 'object'
    return 'array'


def _fit_enum(out):
    """Give an enum a type (Magpie's fitEnum): the one its values have, or that
    and "null"; values of several types become an anyOf of one typed enum for
    each. An empty enum fits nothing and is left out."""
    if 'enum' not in out: return
    values=out['enum']
    if not isinstance(values,list) or not values:
        out.pop('enum',None);return
    listed=out['type'] if isinstance(out.get('type'),list) else [out['type']] if isinstance(out.get('type'),str) else []
    if len([t for t in listed if t!='null'])==1: return
    by_type={}
    for v in values: by_type.setdefault(_enum_type(v),[]).append(v)
    if 'integer' in by_type and 'number' in by_type:
        by_type['number']=by_type['number']+by_type.pop('integer')
    has_null=by_type.pop('null',None) is not None
    if len(by_type)<=1:
        kind=next(iter(by_type),'null')
        out['type']=[kind,'null'] if has_null and kind!='null' else kind
        return
    split=[{'type':kind,'enum':vs} for kind,vs in by_type.items()]
    if has_null: split.append({'type':'null'})
    out.pop('enum',None);out.pop('type',None)
    if isinstance(out.get('anyOf'),list):
        out['allOf']=[*(out['allOf'] if isinstance(out.get('allOf'),list) else []),{'anyOf':split}]
        out.pop('anyOf',None)
    else: out['anyOf']=split


def schema(value, depth=0):
    """Fit common tool schemas without removing enum/required constraints.

    Magpie's fitSchema/fitShape/fitEnum: a pattern's \\0 is \\u0000, a \\
    before it left, so an escaped backslash-0 stays; a tuple's items (a list)
    becomes one schema any of them fits; a type JSON Schema doesn't name is
    left out, lowercased first; a required that isn't a list, or its entries
    that aren't strings, are left out; an enum gets a type, or an anyOf of
    typed enums, at any depth."""
    if depth>48: raise ValueError('工具参数 schema 嵌套过深')
    if value is True: return {}
    if value is False: return {'not':{}}
    if not isinstance(value,dict): return {}
    out=copy.deepcopy(value)
    if isinstance(out.get('pattern'),str):
        out['pattern']=re.sub(r'\\\\|\\0(?![0-9])',lambda m:m.group(0) if m.group(0)=='\\\\' else '\\u0000',out['pattern'])
    if isinstance(out.get('items'),list):
        schemas=[v for v in out['items'] if isinstance(v,dict)]
        out['items']={} if not schemas else schemas[0] if len(schemas)==1 else {'anyOf':schemas}
        out.pop('additionalItems',None)
    if 'type' in out:
        allowed={'string','number','integer','boolean','object','array','null'}
        listed=out['type'] if isinstance(out['type'],list) else [out['type']]
        keep=[]
        for t in listed:
            if isinstance(t,str):
                t=t.lower()
                if t in allowed and t not in keep: keep.append(t)
        if not keep: out.pop('type',None)
        elif len(keep)==1: out['type']=keep[0]
        else: out['type']=keep
    if 'required' in out:
        if not isinstance(out['required'],list): out.pop('required',None)
        else:
            strings=[k for k in out['required'] if isinstance(k,str)]
            if strings: out['required']=strings
            else: out.pop('required',None)
    _fit_enum(out)
    for key in ('properties','patternProperties','$defs','definitions','dependentSchemas'):
        if isinstance(out.get(key),dict): out[key]={k:schema(v,depth+1) for k,v in out[key].items()}
    for key in ('anyOf','oneOf','allOf','prefixItems'):
        if isinstance(out.get(key),list): out[key]=[schema(v,depth+1) for v in out[key]]
    for key in ('items','additionalItems','unevaluatedItems','not','if','then','else','contains','propertyNames'):
        if key in out and not isinstance(out[key],list):
            out[key]=schema(out[key],depth+1)
    for key in ('additionalProperties','unevaluatedProperties'):
        if isinstance(out.get(key),dict): out[key]=schema(out[key],depth+1)
    return out


def object_root(ps, root=None, depth=0):
    """Make a tool's parameters an object at the root (Magpie's objectRoot).

    allOf's branches merge whole; of anyOf's and oneOf's object branches the
    properties merge, a field each branch requires stays required, and a field
    with differing schemas becomes an anyOf of them. A root with no type, or
    an empty one, is an object; local $refs resolve before merging."""
    if root is None: root=ps
    if not isinstance(ps,dict): return {'type':'object','properties':{}}
    unions=[k for k in ('allOf','anyOf','oneOf') if isinstance(ps.get(k),list)]
    if ps.get('type')=='object' and not unions: return ps
    props=dict(ps['properties']) if isinstance(ps.get('properties'),dict) else {}
    required=list(ps['required']) if isinstance(ps.get('required'),list) else []
    def resolve(branch):
        ref=branch.get('$ref') if isinstance(branch,dict) else None
        if isinstance(ref,str) and ref.startswith('#/'):
            at=root
            for key in ref[2:].split('/'):
                at=at.get(key.replace('~1','/').replace('~0','~')) if isinstance(at,dict) else None
            if isinstance(at,dict): return at
        return branch
    def expand(branch):
        if depth<8 and isinstance(branch,dict) and any(isinstance(branch.get(k),list) for k in ('allOf','anyOf','oneOf')):
            return object_root(copy.deepcopy(branch),root,depth+1)
        return branch
    for branch in ps.get('allOf') or []:
        merged=expand(resolve(branch))
        if not isinstance(merged,dict): continue
        for key,value in (merged.get('properties') or {}).items(): props.setdefault(key,value)
        if isinstance(merged.get('required'),list): required+=merged['required']
    branches=[]
    for key in ('anyOf','oneOf'):
        for branch in ps.get(key) or []:
            merged=expand(resolve(branch))
            if not isinstance(merged,dict): continue
            if merged.get('type')!='object' and not isinstance(merged.get('properties'),dict): continue
            branches.append(merged)
    from_map={}
    for branch in branches:
        for key,value in (branch.get('properties') or {}).items():
            if key in props: continue
            seen=from_map.setdefault(key,[])
            if not any(json.dumps(x,ensure_ascii=False,sort_keys=True)==json.dumps(value,ensure_ascii=False,sort_keys=True) for x in seen): seen.append(value)
    for key,variants in from_map.items(): props[key]=variants[0] if len(variants)==1 else {'anyOf':variants}
    if branches:
        for key in (branches[0].get('properties') or {}):
            if all(isinstance(b.get('required'),list) and key in b['required'] for b in branches): required.append(key)
    out={k:v for k,v in ps.items() if k not in unions}
    out['type']='object';out['properties']=props
    keep=[]
    for key in required:
        if isinstance(key,str) and key in props and key not in keep: keep.append(key)
    if keep: out['required']=keep
    else: out.pop('required',None)
    return out


AFTER_TOOLS_REFUSED=re.compile(r'must end with a user message|["\']code["\']\s*:\s*["\']?11153(?![\d.])',re.I)


def after_tools_separator(messages):
    """Insert an empty assistant reply between trailing tool results and the
    user's message(s) (Magpie's replyAfterTools).

    WorkBuddy AI answers 400 code 11153 "the conversation must end with a user
    message" to that shape (gemini-3.8-flash). With the empty reply inserted it
    serves the user's message as the user's, where folded into the tool's
    result it was read as the tool's output. A chat ending with the assistant's
    message is a prefill and stays as it is. Returns whether the pattern was
    present; already-separated chats match nothing."""
    i=len(messages)-1
    while i>=0 and isinstance(messages[i],dict) and messages[i].get('role')=='user': i-=1
    if i<0 or i==len(messages)-1 or not isinstance(messages[i],dict) or messages[i].get('role')!='tool': return False
    messages.insert(i+1,{'role':'assistant','content':''})
    return True


def to_chat(body, protocol, previous=None, *, normalize_client_metadata=True, preserve_tool_parts=False, preserve_reasoning_summary=False):
    if 'stream' in body and type(body['stream']) is not bool: raise ValueError('stream 必须为布尔值')
    if body.get('n',1)!=1: raise ValueError('WorkBuddy AI 每次只支持一个候选回复')
    if body.get('background'): raise ValueError('WorkBuddy AI 不支持后台 Responses 请求')
    messages=copy.deepcopy(previous or [])
    tools=[];identities={};custom=set()
    if protocol=='chat':
        messages+=copy.deepcopy(body.get('messages') or [])
        tools=copy.deepcopy(body.get('tools') or [])
    elif protocol=='anthropic':
        if body.get('system'): messages.append({'role':'system','content':text(body['system'])})
        for message in body.get('messages') or []:
            parts=message.get('content')
            row={'role':message['role'],'content':content(parts)}
            if isinstance(parts,list):
                calls=[{'id':p['id'],'type':'function','function':{'name':p['name'],'arguments':json.dumps(p.get('input') or {},ensure_ascii=False)}} for p in parts if p.get('type')=='tool_use']
                if calls: row['tool_calls']=calls
                thinking=''.join(p.get('thinking','') for p in parts if p.get('type')=='thinking')
                if thinking: row['reasoning_content']=thinking
                results=[p for p in parts if p.get('type')=='tool_result']
                if results:
                    for p in results:
                        result=p.get('content','')
                        if preserve_tool_parts and isinstance(result,list):
                            if any(not isinstance(v,dict) or v.get('type') not in ('text','input_text','output_text','image','image_url','input_image') for v in result):
                                raise ValueError('工具结果包含无法安全迁移的内容类型')
                            value=content(result)
                            if p.get('is_error'):
                                value=[{'type':'text','text':'Error:'}]+(value if isinstance(value,list) else [{'type':'text','text':value}])
                        else:value=('Error: ' if p.get('is_error') else '')+text(result)
                        messages.append({'role':'tool','tool_call_id':p['tool_use_id'],'content':value})
                    images=[v for p in results if isinstance(p.get('content'),list) for v in p['content'] if v.get('type')=='image']
                    if images and not preserve_tool_parts:
                        row['content']=([{'type':'text','text':row['content']}] if isinstance(row['content'],str) and row['content'] else row['content'] if isinstance(row['content'],list) else []) + [{'type':'text','text':'Attached image(s) from tool result:'}]+content(images)
                    if not row['content']: continue
            messages.append(row)
        tools=[{'type':'function','function':{'name':t['name'],'description':t.get('description',''),'parameters':t.get('input_schema',{})}} for t in body.get('tools',[]) if 'name' in t]
        if any('name' not in t for t in body.get('tools',[])): raise ValueError('WorkBuddy AI 仅支持客户端执行的函数工具')
    else:
        if body.get('instructions'): messages.append({'role':'system','content':text(body['instructions'])})
        inputs=body.get('input') or []
        if isinstance(inputs,str): inputs=[{'role':'user','content':inputs}]
        for item in inputs:
            typ=item.get('type','message')
            if typ=='reasoning':
                if preserve_reasoning_summary:
                    if item.get('encrypted_content'):raise ValueError('加密推理无法跨供应商迁移；请使用文本摘要并新建会话')
                    summary=item.get('summary') or []
                    if not isinstance(summary,list) or any(not isinstance(p,dict) or p.get('type')!='summary_text' or not isinstance(p.get('text'),str) for p in summary):
                        raise ValueError('推理摘要格式无法安全迁移')
                    value='\n'.join(p['text'] for p in summary if p.get('text'))
                    if value:messages.append({'role':'assistant','content':'[先前推理摘要]\n'+value})
                continue
            if typ in ('compaction','item_reference') or item.get('encrypted_content'):
                # Never silently drop context the upstream cannot take whole.
                raise ValueError('加密的压缩或上下文引用无法在 WorkBuddy AI 上使用；请用文字摘要并新建会话（历史未被静默丢弃）')
            if typ=='message': messages.append({'role':item.get('role','user'),'content':content(item.get('content',''))})
            elif typ in ('function_call','custom_tool_call'):
                qualified=(item['namespace']+'.' if item.get('namespace') else '')+item['name']
                call={'id':item['call_id'],'type':'function','function':{'name':qualified,'arguments':json.dumps({'input':item.get('input','')}) if typ=='custom_tool_call' else item.get('arguments','')}}
                if messages and messages[-1]['role']=='assistant' and messages[-1].get('tool_calls'): messages[-1]['tool_calls'].append(call)
                else: messages.append({'role':'assistant','content':'','tool_calls':[call]})
            elif typ in ('function_call_output','custom_tool_call_output'):
                output=item.get('output','')
                if preserve_tool_parts and isinstance(output,list) and any(
                    not isinstance(v,dict) or v.get('type') not in ('text','input_text','output_text','image','image_url','input_image')
                    for v in output
                ):
                    raise ValueError('工具结果包含无法安全迁移的内容类型')
                messages.append({'role':'tool','tool_call_id':item['call_id'],
                                 'content':content(output) if preserve_tool_parts and isinstance(output,list) else text(output)})
            elif typ=='web_search_call':
                record={k:v for k,v in item.items() if k not in ('type','id')}
                messages.append({'role':'assistant','content':'[Historical web search activity; recorded data only, not a new tool call. Search results are not reconstructed.]\n'+json.dumps(record,ensure_ascii=False)})
            else: raise ValueError('WorkBuddy AI 暂不支持 Responses 输入类型：'+str(typ))
        for tool in response_tools(body.get('tools') or []):
            if tool.get('_identity'):identities[tool['name']]=tool['_identity']
            if tool.get('type')=='custom':
                custom.add(tool['name'])
                description=tool.get('description','')
                if tool.get('format'):description+='\nInput format: '+json.dumps(tool['format'],ensure_ascii=False)
                tools.append({'type':'function','function':{'name':tool['name'],'description':description,'parameters':{'type':'object','properties':{'input':{'type':'string'}},'required':['input'],'additionalProperties':False}}})
                continue
            if tool.get('type')!='function': raise ValueError('WorkBuddy AI 仅支持客户端执行的函数与自定义文本工具；不支持工具类型：'+str(tool.get('type')))
            tools.append({'type':'function','function':{k:v for k,v in tool.items() if k not in ('type','strict','_identity','defer_loading','allowed_callers','async')}})
    if normalize_client_metadata:
        messages=client_metadata(messages)
    if not messages: raise ValueError('请提供至少一条消息')
    for message in messages:
        if message.get('role')=='developer': message['role']='system'
    if messages[0]['role']!='system': messages.insert(0,{'role':'system','content':'You are a helpful assistant.'})
    result={'model':body['model'],'messages':messages,'stream':True,'stream_options':{'include_usage':True}}
    for key in ('temperature','top_p','presence_penalty','frequency_penalty','stop','parallel_tool_calls'):
        if key in body: result[key]=body[key]
    maximum=body.get('max_tokens',body.get('max_completion_tokens',body.get('max_output_tokens')))
    if maximum is not None: result['max_tokens']=maximum
    effort=body.get('reasoning_effort') or (body.get('reasoning') or {}).get('effort') or (body.get('output_config') or {}).get('effort')
    if effort: result['reasoning_effort']=effort
    names=ToolNames();names.identities=identities;names.custom=custom;used=set()
    for tool in tools:
        fn=tool.get('function') or {}
        original=fn.get('name','')
        if not original: raise ValueError('工具名称不能为空')
        safe=re.sub(r'[^A-Za-z0-9_-]','_',original)[:64]
        if safe in used: safe=safe[:47]+'_'+hashlib.sha256(original.encode()).hexdigest()[:16]
        if safe in used: raise ValueError('工具名称重复')
        used.add(safe);names[safe]=original;fn['name']=safe
        fn.pop('strict',None)
        fn['parameters']=object_root(schema(fn.get('parameters',{})))
    if tools: result['tools']=tools
    reverse={v:k for k,v in names.items()}
    for message in messages:
        for tc in message.get('tool_calls') or []:
            fn=tc.get('function') or {};fn['name']=reverse.get(fn.get('name'),fn.get('name'))
    choice=body.get('tool_choice')
    if isinstance(choice,dict):
        kind=choice.get('type')
        namespace=choice.get('namespace')
        choice=(choice.get('function') or {}).get('name') or choice.get('name') if kind in ('function','tool','custom') else 'required' if kind=='any' else kind
        if namespace and kind in ('function','custom'):choice=namespace+'.'+choice
    if choice=='none': result.pop('tools',None)
    elif choice: result['tool_choice']=reverse.get(choice,choice)
    return result,names


def conversation_headers(chat, session, salt):
    def signature(m):
        return json.dumps(m.get('content',''),ensure_ascii=False,sort_keys=True)
    users=[(i,m) for i,m in enumerate(chat['messages']) if m.get('role')=='user' and not (
        i and chat['messages'][i-1].get('role')=='tool' and isinstance(m.get('content'),list) and m['content'] and
        m['content'][0].get('text')=='Attached image(s) from tool result:')]
    conv='session:'+session if session else 'first:'+signature(users[0][1]) if users else ''
    turn=str(users[-1][0])+':'+signature(users[-1][1]) if users else ''
    digest=lambda key:hashlib.sha256((salt+'|'+key).encode()).hexdigest()[:32]
    mid=secrets.token_hex(16)
    result={'X-Conversation-Message-ID':mid,'X-Request-ID':mid,
            'X-Conversation-Request-ID':digest('turn|'+conv+'|'+turn) if turn else mid}
    if conv: result['X-Conversation-ID']=digest('conversation|'+conv)
    return result


def sse_events(response, label='WorkBuddy AI', *, allow_errors=False):
    pending=b''; data=[]
    while True:
        chunk=response.read1(65536)
        if not chunk: break
        pending+=chunk
        if len(pending)>8*1024*1024: raise ValueError(label+' 流事件超过大小限制')
        while b'\n' in pending:
            line,pending=pending.split(b'\n',1);line=line.rstrip(b'\r')
            if line.startswith(b'data:'): data.append(line[5:].lstrip())
            elif not line and data:
                raw=b'\n'.join(data);data=[]
                if raw==b'[DONE]': return
                value=json.loads(raw)
                if (value.get('error') or value.get('code')) and not allow_errors: raise ValueError(label+' 在流中返回错误')
                yield value
            if sum(map(len,data))>8*1024*1024: raise ValueError(label+' 流事件超过大小限制')


class Reply:
    """Incremental output events plus a bounded complete reply for non-streaming clients."""
    def __init__(self, protocol, model, names, custom_tools=()):
        self.protocol,self.model,self.names=protocol,model,names
        self.served=None # the upstream's own spelling, for the ledger only
        self.custom_tools=set(custom_tools)|getattr(names,'custom',set())
        self.id=('msg_' if protocol=='anthropic' else 'resp_' if protocol=='responses' else 'chatcmpl_')+uuid.uuid4().hex
        self.created=int(time.time()); self.started=False; self.finished=False; self.reason='stop'
        self.usage={}; self.blocks={}; self.output=[]; self.calls={}; self.chars=0; self.seq=0

    def event(self, typ, **data):
        value={'type':typ,**data}
        if self.protocol=='responses': value['sequence_number']=self.seq;self.seq+=1
        return value

    def base(self, status='in_progress'):
        return {'id':self.id,'object':'response','created_at':self.created,'model':self.model,
                'status':status,'output':copy.deepcopy(self.output),'parallel_tool_calls':True,
                'usage':self.response_usage(),'error':None,'incomplete_details':{'reason':'max_output_tokens'} if status=='incomplete' else None}

    def response_usage(self):
        u=self.usage; p=u.get('prompt_tokens',0); c=u.get('completion_tokens',0)
        return {'input_tokens':p,'output_tokens':c,'total_tokens':u.get('total_tokens',p+c),
                'input_tokens_details':{'cached_tokens':(u.get('prompt_tokens_details') or {}).get('cached_tokens',0)},
                'output_tokens_details':{'reasoning_tokens':(u.get('completion_tokens_details') or {}).get('reasoning_tokens',0)}}

    def anthropic_usage(self):
        u=self.usage; cached=(u.get('prompt_tokens_details') or {}).get('cached_tokens',0)
        return {'input_tokens':max(0,u.get('prompt_tokens',0)-cached),'output_tokens':u.get('completion_tokens',0),
                'cache_read_input_tokens':cached,'cache_creation_input_tokens':0}

    def start(self):
        if self.started: return []
        self.started=True
        if self.protocol=='anthropic': return [self.event('message_start',message={'id':self.id,'type':'message','role':'assistant','model':self.model,'content':[], 'stop_reason':None,'stop_sequence':None,'usage':self.anthropic_usage()})]
        if self.protocol=='responses': return [self.event('response.created',response=self.base()),self.event('response.in_progress',response=self.base())]
        return []

    def block(self, key, typ, tc=None):
        if key in self.blocks: return self.blocks[key],[]
        index=len(self.output); ident=('fc_' if typ=='tool' else 'rs_' if typ=='thinking' else 'msg_')+uuid.uuid4().hex
        block={'index':index,'type':typ,'text':'','id':ident}
        if tc:
            block.update(call_id=tc.get('id') or 'call_'+uuid.uuid4().hex,name=self.names.get(tc['function'].get('name'),tc['function'].get('name','')),arguments='')
            block['custom']=block['name'] in self.custom_tools
        self.blocks[key]=block
        if self.protocol=='anthropic':
            value={'type':'text','text':''} if typ=='text' else {'type':'thinking','thinking':''} if typ=='thinking' else {'type':'tool_use','id':block['call_id'],'name':block['name'],'input':{}}
            self.output.append(value)
            return block,[self.event('content_block_start',index=index,content_block=copy.deepcopy(value))]
        if self.protocol=='responses':
            value={'id':ident,'type':'message','status':'in_progress','role':'assistant','content':[]} if typ=='text' else {'id':ident,'type':'reasoning','summary':[]} if typ=='thinking' else {'id':ident,'type':'function_call','status':'in_progress','call_id':block['call_id'],'name':block['name'],'arguments':''}
            if typ=='tool':value.update(getattr(self.names,'identities',{}).get(block['name'],{}))
            if block.get('custom'):
                value.pop('arguments',None);value.update(type='custom_tool_call',input='')
            self.output.append(value)
            events=[self.event('response.output_item.added',output_index=index,item=copy.deepcopy(value))]
            if typ=='text': events.append(self.event('response.content_part.added',item_id=ident,output_index=index,content_index=0,part={'type':'output_text','text':'','annotations':[]}))
            if typ=='thinking': events.append(self.event('response.reasoning_summary_part.added',item_id=ident,output_index=index,summary_index=0,part={'type':'summary_text','text':''}))
            return block,events
        self.output.append(block)
        return block,[]

    def feed(self, chunk):
        # The reply names the model the client asked for, never the upstream's
        # own spelling (an agent that reads the name back, as Claude Code does
        # for its context window, sees what it asked for). The upstream's
        # spelling is kept for the ledger as `served`.
        if chunk.get('model'): self.served=chunk['model']
        events=self.start()
        if chunk.get('usage'): self.usage=chunk['usage']
        # Preserve the upstream response id for chat; converted protocols use
        # their own stable id, also recorded by the gateway observer.
        if self.protocol=='chat':
            value=copy.deepcopy(chunk);value['id']=self.id;value['model']=self.model
            for choice in value.get('choices') or []:
                for tc in (choice.get('delta') or {}).get('tool_calls') or []:
                    fn=tc.get('function') or {}
                    if fn.get('name'): fn['name']=self.names.get(fn['name'],fn['name'])
            events.append(value)
        for choice in (chunk.get('choices') or [])[:1]:
            if choice.get('finish_reason'):
                self.finished=True;self.reason=choice['finish_reason']
            delta=choice.get('delta') or {}
            for key,typ in [('reasoning_content','thinking'),('content','text')]:
                value=delta.get(key)
                if not isinstance(value,str) or not value: continue
                self.chars+=len(value)
                b,opened=self.block(typ,typ);events+=opened;b['text']+=value
                if self.protocol=='anthropic': events.append(self.event('content_block_delta',index=b['index'],delta={'type':'text_delta','text':value} if typ=='text' else {'type':'thinking_delta','thinking':value}))
                elif self.protocol=='responses': events.append(self.event('response.output_text.delta' if typ=='text' else 'response.reasoning_summary_text.delta',item_id=b['id'],output_index=b['index'],**({'content_index':0} if typ=='text' else {'summary_index':0}),delta=value))
            for tc in delta.get('tool_calls') or []:
                index=tc.get('index',0); current=self.calls.setdefault(index,{'id':'','function':{'name':'','arguments':''}})
                if tc.get('id'): current['id']=tc['id']
                fn=tc.get('function') or {}
                if fn.get('name'): current['function']['name']+=fn['name']
                args=fn.get('arguments') or '';current['function']['arguments']+=args;self.chars+=len(args)
                if not current['function']['name']: continue
                key='tool-'+str(index);first=key not in self.blocks
                b,opened=self.block(key,'tool',current);events+=opened
                value=current['function']['arguments'] if first else args
                b['arguments']+=value
                if value and self.protocol=='anthropic': events.append(self.event('content_block_delta',index=b['index'],delta={'type':'input_json_delta','partial_json':value}))
                elif value and self.protocol=='responses' and not b.get('custom'): events.append(self.event('response.function_call_arguments.delta',item_id=b['id'],output_index=b['index'],delta=value))
        if self.chars>8*1024*1024: raise ValueError('WorkBuddy AI 回复超过 8 MiB 限制')
        return events

    def end(self):
        if not self.finished: raise ValueError('WorkBuddy AI 流提前结束，未收到完成标记')
        events=[]
        for b in self.blocks.values():
            typ=b['type'];i=b['index'];out=self.output[i]
            if typ=='tool':
                try: args=json.loads(b['arguments'] or '{}')
                except ValueError: raise ValueError('WorkBuddy AI 返回无效工具参数') from None
                if self.protocol=='anthropic': out['input']=args
                elif self.protocol=='responses':
                    if b.get('custom'):
                        if not isinstance(args,dict) or not isinstance(args.get('input'),str):raise ValueError('WorkBuddy AI 返回无效自定义工具输入')
                        out['input']=args['input']
                    else:out['arguments']=b['arguments']
                    out['status']='completed'
            elif self.protocol=='anthropic': out['text' if typ=='text' else 'thinking']=b['text']
            elif self.protocol=='responses':
                if typ=='text': out.update(status='completed',content=[{'type':'output_text','text':b['text'],'annotations':[]}])
                else: out['summary']=[{'type':'summary_text','text':b['text']}]
            if self.protocol=='anthropic': events.append(self.event('content_block_stop',index=i))
            elif self.protocol=='responses':
                common={'item_id':b['id'],'output_index':i}
                if typ=='tool':
                    if b.get('custom'):
                        events.append(self.event('response.custom_tool_call_input.delta',**common,delta=out['input']))
                        events.append(self.event('response.custom_tool_call_input.done',**common,input=out['input']))
                    else:events.append(self.event('response.function_call_arguments.done',**common,name=out['name'],arguments=b['arguments']))
                elif typ=='text':
                    events.append(self.event('response.output_text.done',**common,content_index=0,text=b['text']))
                    events.append(self.event('response.content_part.done',**common,content_index=0,part=copy.deepcopy(out['content'][0])))
                else:
                    events.append(self.event('response.reasoning_summary_text.done',**common,summary_index=0,text=b['text']))
                    events.append(self.event('response.reasoning_summary_part.done',**common,summary_index=0,part=copy.deepcopy(out['summary'][0])))
                events.append(self.event('response.output_item.done',output_index=i,item=copy.deepcopy(out)))
        if self.protocol=='anthropic':
            events += [self.event('message_delta',delta={'stop_reason':self.stop_reason(),'stop_sequence':None},usage=self.anthropic_usage()),self.event('message_stop')]
        elif self.protocol=='responses':
            status='incomplete' if self.reason=='length' else 'completed'
            events.append(self.event('response.'+status,response=self.base(status)))
        return events

    def stop_reason(self):
        return 'tool_use' if self.reason in ('tool_calls','function_call') else 'max_tokens' if self.reason=='length' else 'end_turn'

    def complete(self):
        if self.protocol=='anthropic': return {'id':self.id,'type':'message','role':'assistant','model':self.model,'content':self.output,'stop_reason':self.stop_reason(),'stop_sequence':None,'usage':self.anthropic_usage()}
        if self.protocol=='responses': return self.base('incomplete' if self.reason=='length' else 'completed')
        msg=self.assistant()
        return {'id':self.id,'object':'chat.completion','created':self.created,'model':self.model,
                'choices':[{'index':0,'message':msg,'finish_reason':self.reason}],'usage':self.usage}

    def assistant(self):
        msg={'role':'assistant','content':''.join(b['text'] for b in self.blocks.values() if b['type']=='text')}
        thinking=''.join(b['text'] for b in self.blocks.values() if b['type']=='thinking')
        if thinking: msg['reasoning_content']=thinking
        calls=[{'id':b['call_id'],'type':'function','function':{'name':b['name'],'arguments':b['arguments']}} for b in self.blocks.values() if b['type']=='tool']
        if calls: msg['tool_calls']=calls
        return msg


def encode_event(protocol, value):
    prefix=('event: '+value['type']+'\n') if protocol!='chat' else ''
    return (prefix+'data: '+json.dumps(value,ensure_ascii=False,separators=(',',':'))+'\n\n').encode()
