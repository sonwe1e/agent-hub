"""Translate the three Hub client protocols to WorkBuddy's streamed chat API.

WorkBuddy request conventions reference magpie-community/plugins (MIT);
see docs/WORKBUDDY-LICENSE.txt. Client protocol conversion is maintained here.
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
        elif typ in ('thinking','redacted_thinking','tool_use','tool_result'): continue
        else: raise ValueError('WorkBuddy AI 暂不支持此消息内容类型：'+str(typ))
    return result or ''


def schema(value, depth=0):
    """Fit common tool schemas without removing enum/required constraints."""
    if depth>48: raise ValueError('工具参数 schema 嵌套过深')
    if value is True: return {}
    if value is False: return {'not':{}}
    if not isinstance(value,dict): return {}
    out=copy.deepcopy(value)
    for key in ('properties','$defs','definitions','patternProperties'):
        if isinstance(out.get(key),dict): out[key]={k:schema(v,depth+1) for k,v in out[key].items()}
    for key in ('anyOf','oneOf','allOf','prefixItems'):
        if isinstance(out.get(key),list): out[key]=[schema(v,depth+1) for v in out[key]]
    for key in ('items','not','if','then','else','contains'):
        if key in out:
            out[key]={'anyOf':[schema(v,depth+1) for v in out[key]]} if isinstance(out[key],list) else schema(out[key],depth+1)
    if isinstance(out.get('additionalProperties'),dict): out['additionalProperties']=schema(out['additionalProperties'],depth+1)
    if isinstance(out.get('pattern'),str): out['pattern']=out['pattern'].replace('\\0','\\u0000')
    if 'required' in out and not isinstance(out['required'],list): out.pop('required')
    if isinstance(out.get('type'),str): out['type']=out['type'].lower()
    allowed={'string','number','integer','boolean','object','array','null'}
    if 'type' in out and (isinstance(out['type'],str) and out['type'] not in allowed): out.pop('type')
    if 'enum' in out:
        if not out['enum']: out.pop('enum')
        elif not out.get('type'):
            kinds={('null' if v is None else 'boolean' if isinstance(v,bool) else 'number' if isinstance(v,(int,float)) else 'string' if isinstance(v,str) else 'object' if isinstance(v,dict) else 'array') for v in out['enum']}
            out['type']=next(iter(kinds)) if len(kinds)==1 else sorted(kinds)
    return out


def to_chat(body, protocol, previous=None):
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
                    messages += [{'role':'tool','tool_call_id':p['tool_use_id'],'content':('Error: ' if p.get('is_error') else '')+text(p.get('content',''))} for p in results]
                    images=[v for p in results if isinstance(p.get('content'),list) for v in p['content'] if v.get('type')=='image']
                    if images:
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
            if typ=='message': messages.append({'role':item.get('role','user'),'content':content(item.get('content',''))})
            elif typ in ('function_call','custom_tool_call'):
                qualified=(item['namespace']+'.' if item.get('namespace') else '')+item['name']
                call={'id':item['call_id'],'type':'function','function':{'name':qualified,'arguments':json.dumps({'input':item.get('input','')}) if typ=='custom_tool_call' else item.get('arguments','')}}
                if messages and messages[-1]['role']=='assistant' and messages[-1].get('tool_calls'): messages[-1]['tool_calls'].append(call)
                else: messages.append({'role':'assistant','content':'','tool_calls':[call]})
            elif typ in ('function_call_output','custom_tool_call_output'): messages.append({'role':'tool','tool_call_id':item['call_id'],'content':text(item.get('output',''))})
            elif typ=='reasoning': continue # Responses' opaque reasoning has no chat equivalent.
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
        params=schema(fn.get('parameters',{}))
        if not params.get('type'):
            # A root union must be an object for WorkBuddy's function tools.
            params['type']='object'
            props=params.setdefault('properties',{})
            for key in ('anyOf','oneOf','allOf'):
                for branch in params.get(key,[]): props.update(branch.get('properties') or {})
        fn['parameters']=params
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


def sse_events(response):
    pending=b''; data=[]
    while True:
        chunk=response.read1(65536)
        if not chunk: break
        pending+=chunk
        if len(pending)>8*1024*1024: raise ValueError('WorkBuddy AI 流事件超过大小限制')
        while b'\n' in pending:
            line,pending=pending.split(b'\n',1);line=line.rstrip(b'\r')
            if line.startswith(b'data:'): data.append(line[5:].lstrip())
            elif not line and data:
                raw=b'\n'.join(data);data=[]
                if raw==b'[DONE]': return
                value=json.loads(raw)
                if value.get('error') or value.get('code'): raise ValueError('WorkBuddy AI 在流中返回错误')
                yield value
            if sum(map(len,data))>8*1024*1024: raise ValueError('WorkBuddy AI 流事件超过大小限制')


class Reply:
    """Incremental output events plus a bounded complete reply for non-streaming clients."""
    def __init__(self, protocol, model, names, custom_tools=()):
        self.protocol,self.model,self.names=protocol,model,names
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
        if chunk.get('model'): self.model=chunk['model']
        events=self.start()
        if chunk.get('usage'): self.usage=chunk['usage']
        # Preserve the upstream response id for chat; converted protocols use
        # their own stable id, also recorded by the gateway observer.
        if self.protocol=='chat':
            value=copy.deepcopy(chunk);value['id']=self.id
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
