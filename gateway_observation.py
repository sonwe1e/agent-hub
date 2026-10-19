"""Normalize Hub gateway ledgers and legacy captures; join exact response IDs."""
import gzip
import json
from pathlib import Path


def speed(record):
    # Same bounds and reasoning/burst exclusions as Magpie usage.DecodeOf.
    out, reasoning = record.get('out',0), record.get('reasoning',0)
    ttft, first_text = record.get('ttft_ms',0), record.get('first_text_ms',0)
    start, flow = ttft, record.get('flow_ms',0)
    if reasoning:
        if first_text:
            out, start = out-reasoning, first_text
        elif record.get('first_reasoning_ms',0)>=ttft and flow>0 and reasoning<=out:
            start=record['first_reasoning_ms']
        else:
            return None
    window=record.get('ms',0)-start
    if ttft<=0 or out<=0 or window<100 or out*1000>10000*window:
        return None
    if flow>0 and flow*4<window:
        return None
    if reasoning and first_text and out*(first_text-ttft)>20*reasoning*window:
        return None
    return out*1000/window


def read_routes(directory):
    from observation import _lines
    routes={}
    for path in sorted((Path(directory)/'routing').glob('*.jsonl*')):
        if path.suffix not in ('.jsonl','.gz'):
            continue
        try:
            with (gzip.open(path,'rb') if path.suffix=='.gz' else path.open('rb')) as f:
                for row in _lines(f):
                    if row.get('done'):
                        routes[row.get('id')]=row
        except (OSError,ValueError):
            continue
    return routes


def read_requests(path, mapping=None, identities=None, secrets=()):
    from observation import open_lines, stamp, key, number, redact
    from agent_inventory import AGENTS
    path=Path(path)
    if not path.exists():
        return []
    if mapping is None:
        try:
            mapping=json.loads((path.parent/'hub-connections.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):
            mapping={}
    groups={}
    routes=read_routes(path.parent)
    for i,row in enumerate(open_lines(path)):
        source_agent=row.get('agent')
        agent={'claude-desktop':'claude','codex-desktop':'codex'}.get(source_agent,source_agent)
        if agent not in AGENTS:
            matches=(identities or {}).get(row.get('response_id'),set())
            agent=next(iter(matches)) if len(matches)==1 else None
        if agent not in AGENTS or row.get('source')=='reported':
            continue
        row={**row,'agent':agent,'sourceAgent':source_agent}
        groups.setdefault(row.get('route_id') or key(str(path),i),[]).append(row)
    result=[]
    for gid, attempts in groups.items():
        finals=[r for r in attempts if not r.get('handed_on')]
        if not finals:
            continue # A handed-on attempt is never an independent final failure.
        r=finals[-1]
        at=stamp(r.get('t'))
        if not at:
            continue
        rid=r.get('response_id') or key('gateway',str(path),gid)
        native_sid=r.get('native_session') or r.get('session') or None
        agent=r['agent']
        sub=r.get('subagent')
        sid=native_sid+f'/agent-{sub}' if native_sid and sub and agent=='claude' else native_sid
        route=routes.get(gid,{})
        provider=r.get('provider') or None
        connection=mapping.get(provider,{})
        model=r.get('req') or r.get('model')
        # Remove only the exact generated connection prefix, never arbitrary slashes.
        if connection.get('agent')==agent:
            prefix=provider+'/'
            model=model[len(prefix):] if model and model.startswith(prefix) else model
            provider=connection['provider']
        u={name:number(r.get(field)) for name,field in [('input','in'),('output','out'),('cacheRead','cache_read'),('cacheWrite','cache_write'),('reasoning','reasoning')]}
        # A refused/aborted all-zero ledger row does not establish actual consumption.
        http=r.get('status',0)
        failed=http>=400 or bool(r.get('err'))
        if failed and not any(u.values()):
            u={k:None for k in u}
        u.update(context=sum(u[x] or 0 for x in ('input','cacheRead','cacheWrite')) if u['input'] is not None else None,
                 inputIncludesCache=False,contextPrecision='measured' if all(u[x] is not None for x in ('input','cacheRead','cacheWrite')) else 'unknown')
        interrupted = r.get('stop') in ('aborted','cancelled') or http==499
        status='interrupted' if interrupted else 'failed' if failed else 'succeeded' if 200<=http<300 and r.get('stop') else 'unknown'
        prompt=route.get('prompt')
        engine='Agent Hub' if r.get('engine')=='agent-hub' else 'Magpie（历史采集）'
        if prompt:
            # Names may contain file/message fragments. Keep only structural groups by default.
            prompt={k:prompt[k] for k in ('tokens','counted','held','turns','window') if k in prompt} | {
                'parts':[{'kind':p.get('kind'),'tokens':p.get('tokens')} for p in prompt.get('parts',[])],
                'method':engine+'：ASCII 约 4 字符/Token，非 ASCII 约 1 字符/Token；有实测总量时按总量缩放分项。分项仍为估算。'}
        n=max(len(attempts),r.get('tries',1),len(route.get('tries',[])))
        summaries=[]
        for a in attempts:
            summaries.append({'provider':mapping.get(a.get('provider'),{}).get('provider',a.get('provider')),
                'model':a.get('model'),'status':a.get('status'),'error':redact(a.get('err',''),secrets) or None,
                'handedOn':bool(a.get('handed_on')), 'durationMs':a.get('ms'),
                'usage':{k:a.get(v) for k,v in [('input','in'),('output','out'),('cacheRead','cache_read'),('cacheWrite','cache_write')]}})
        gaps=['费用是模型参考价估算，非供应商实际扣费。','正文额外留存默认关闭；分项 Token 为 '+engine+' 结构估算。']
        if r.get('engine')=='agent-hub':gaps.append('网关当前不自动重试。客户端重发若无可靠关联 ID，保留为独立请求，无法确认逻辑重试次数。')
        if r.get('observationGap'):gaps.append(r['observationGap'])
        if status=='unknown':gaps.append('HTTP 状态已记录，但该网关账本没有明确的流完成 / stop 标记；不把 HTTP 200 单独当作完整回复的证明。')
        result.append({'id':key(agent,rid),'nativeId':rid,'agent':agent,'sessionId':sid,'parentSessionId':native_sid if sub else None,
            'time':at,'model':model,'wireModel':r.get('req'),'servedModel':r.get('served') or None,'provider':provider,
            'providerEvidence':'gateway-upstream','status':status,'httpStatus':http,
            'error':redact(r.get('err',''),secrets) or None,'durationMs':number(r.get('ms')),'ttftMs':number(r.get('ttft_ms')),
            'firstTextMs':number(r.get('first_text_ms')),'outputSpeed':speed(r),'usage':u,
            'window':number((prompt or {}).get('window')),'windowEvidence':'gateway-catalog' if (prompt or {}).get('window') else None,
            'kind':r.get('kind') or 'response','attempts':n,'retried':n>1,'attemptDetails':summaries,
            'routeId':gid,'cost':None,'costEvidence':None,'source':str(path),'sourceType':'gateway',
            'evidence':engine+' usage.jsonl + routing；按 response_id 与原生记录合并', 'contextParts':prompt,'sourceAgent':r.get('sourceAgent'),
            'gaps':gaps})
    return result
