"""Offline audit of scrubbed REAL captures from verify-live-clients.py.

Builds a fresh, isolated native-source tree and runs the production collector.
Exports the same SQLite index for real-browser review. No client/API is run.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from observation import Store, parse_native, open_lines, fresh_input


def dump(path, value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


def capture_evidence(folder):
    result_file = folder/'result.json'
    result = json.loads(result_file.read_text(encoding='utf-8')) if result_file.exists() else {}
    events = []
    for line in (folder/'stdout.jsonl').read_text(encoding='utf-8').splitlines():
        try:
            events.append(json.loads(json.loads(line)['line']))
        except ValueError:
            pass
    agent = folder.name.split('-')[0]
    finals, tool_ids, deltas = [], [], 0
    for e in events:
        typ = e.get('type') or e.get('method')
        m = e.get('message') or {}
        item = e.get('item') or (e.get('params') or {}).get('item') or {}
        if agent == 'claude':
            if typ == 'result' and not e.get('is_error'):
                finals.append(e.get('result',''))
            if typ == 'user':
                tool_ids.extend(x.get('tool_use_id') for x in m.get('content',[]) if isinstance(x,dict) and x.get('type')=='tool_result' and not x.get('is_error'))
            deltas += typ == 'stream_event' and (e.get('event') or {}).get('type')=='content_block_delta' and (e['event'].get('delta') or {}).get('type')=='text_delta'
        elif agent == 'pi':
            if typ == 'message_end' and m.get('role')=='assistant' and m.get('stopReason')=='stop':
                finals.append(''.join(x.get('text','') for x in m.get('content',[]) if x.get('type')=='text'))
            if typ == 'tool_execution_end' and not e.get('isError'):
                tool_ids.append(e.get('toolCallId'))
            deltas += typ == 'message_update' and (e.get('assistantMessageEvent') or {}).get('type')=='text_delta'
        elif agent == 'dsh':
            if typ == 'final':
                finals.append(e.get('text',''))
            if typ == 'tool_result' and e.get('status')=='completed':
                tool_ids.append(e.get('callId'))
            deltas += typ == 'text'
        else:
            if typ in ('item.completed','item/completed') and item.get('type') in ('agent_message','agentMessage'):
                finals.append(item.get('text',''))
            if typ in ('item.completed','item/completed') and item.get('type') in ('command_execution','commandExecution') and item.get('exit_code',item.get('exitCode'))==0:
                tool_ids.append(item.get('id'))
            deltas += typ == 'item/agentMessage/delta'
    nonce = result.get('nonce')
    if not nonce:
        match = re.search(r'HUB_READ_[0-9a-f]+',json.dumps(events))
        nonce = match[0] if match else None
    return {'capture':str(folder.relative_to(ROOT)), 'agent':agent, 'auth':result.get('auth'),
            'exitCode':result.get('exitCode'), 'timedOut':result.get('timedOut'),
            'originalFilesUnchanged':result.get('originalFilesUnchanged'),
            'probeKind':result.get('probeKind','read'),
            'finalNonceVerified':bool(nonce and any(nonce in t for t in finals)),
            'successfulToolResults':len(tool_ids),'textDeltaEvents':deltas,
            'cleanup':result.get('cleanup')}


def verify(captures, out):
    out.mkdir(parents=True,exist_ok=False)
    home = out/'native-home'
    home.mkdir()
    checks, skipped, cases, expected = [], [], [], {}
    def check(condition, description):
        if not condition:
            raise AssertionError(description)
        checks.append(description)
    for native in sorted(captures.glob('????????-??????/*/native')):
        folder = native.parent
        agent = folder.name.split('-')[0]
        if agent not in ('claude','codex','pi','dsh'):
            continue
        cases.append(capture_evidence(folder))
        for source in native.rglob('*.jsonl'):
            parsed = parse_native(agent,source,True)
            if any(r['model']=='[REDACTED]' or r['servedModel']=='[REDACTED]' for r in parsed.requests.values()):
                skipped.append({'source':str(source.relative_to(ROOT)), 'reason':'early harness redacted a non-secret model identifier; superseded by a fresh capture'})
                continue
            dest = home/source.relative_to(native)
            dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,dest)
            for r in parsed.requests.values():
                expected[r['id']] = r
    check({r['agent'] for r in expected.values()}=={'claude','codex','pi','dsh'},'真实原生日志覆盖四个 Agent')
    for agent in ('claude','codex','pi','dsh'):
        check(any(c['agent']==agent and c['finalNonceVerified'] and c['successfulToolResults'] for c in cases),agent+' 真实工具结果与最终随机值一致')
        check(any(c['agent']==agent and c['textDeltaEvents'] for c in cases),agent+' 客户端有实际文本增量事件')
    store = Store(out/'data',home)
    try:
        dump(store.settings_path,{'enabled':True,'content':True,'retentionDays':30,'clearedBefore':0})
        env = {'CLAUDE_CONFIG_DIR':'','CODEX_HOME':'','PI_CODING_AGENT_DIR':'','DSH_HOME':''}
        with patch.dict(os.environ,env):
            store.collect()
        check(store.progress['state']=='complete' and not store.progress['errors'],'正式采集器无解析失败')
        data = store.query({'days':30,'pageSize':100})
        check(data['total']==len(expected),'概览与原生调用 ID 去重总数一致')
        check(data['summary']['calls']==sum(s['summary']['calls'] for s in data['sessions']),'概览与会话调用数一致')
        check(data['total']==sum(d['calls'] for d in data['distribution']['agent']),'Agent 分布与请求数量一致')
        for request in data['requests']:
            detail = store.detail(request['id'])
            check(detail['usage']==expected[request['id']]['usage'],'原生 Token 与详情一致 '+request['id'])
            session = store.session_detail(request['agent'],request['sessionId'],{'days':30})
            point = next(p for p in session['session']['points'] if p['id']==request['id'])
            check(point['input']==detail['usage']['context'],'单次上下文点对应调用 '+request['id'])
            check(point['cacheRead']==detail['usage']['cacheRead'],'上下文缓存不重复加总 '+request['id'])
        for dim in ('input','output','reasoning','cacheRead','cacheWrite','context'):
            vals = [fresh_input(r['usage']) if dim=='input' else r['usage'][dim] for r in expected.values()]
            vals = [v for v in vals if v is not None]
            check(data['summary']['tokens'][dim]['value']==(sum(vals) if vals else None),dim+' 总量与明细一致')
        check(any(r['status']=='failed' and r['agent']=='pi' and 'not supported' in (r['error'] or '') for r in data['requests']),'真实模型拒绝失败可从概览定位到 pi 请求')
        count = data['total']
        with patch.dict(os.environ,env):
            store.collect()
        check(store.query({'days':30})['total']==count,'重复采集不增加调用数')
        # Production API/UI receives precisely this data, without seeded totals.
        dump(out/'observation.json',data)
        evidence = {'capturedAt':time.time(),'checks':checks,'cases':cases,'skipped':skipped,
                    'summary':data['summary'],'agents':data['distribution']['agent'],
                    'sessions':data['sessionTotal'],'requests':data['total']}
        dump(out/'verification.json',evidence)
        # Stop later automatic scanning. These logs contain only the harmless
        # test task; retaining content here does not change live Hub settings.
        dump(store.settings_path,{**store.settings(),'enabled':False})
        print(json.dumps({'report':str(out),'checks':len(checks),'requests':count,'sessions':data['sessionTotal'],
                          'summary':data['summary'],'skipped':len(skipped)},ensure_ascii=False),flush=True)
    finally:
        store.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--capture-root',type=Path,default=ROOT/'tests/artifacts/live-clients')
    p.add_argument('--out',type=Path,required=True)
    args=p.parse_args()
    verify(args.capture_root.resolve(),args.out.resolve())
