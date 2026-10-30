"""Offline audit of real gateway + native captures; no vendor requests are made."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from agent_hub.observability.store import Store, parse_native


def dump(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


def verify(cases,out):
    out.mkdir(parents=True,exist_ok=False)
    home=out/'native-home';home.mkdir()
    store=Store(out/'data',home)
    checks=[]
    def check(ok,description):
        if not ok:raise AssertionError(description)
        checks.append(description)
    try:
        results=[]
        native_ids=set()
        for case in cases:
            result=json.loads((case/'result.json').read_text(encoding='utf-8'))
            result['capture']=str(case)
            results.append(result)
            for path in (case/'native').rglob('*.jsonl'):
                dest=home/path.relative_to(case/'native')
                dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,dest)
                native_ids.update(parse_native(result['agent'],dest).requests)
        dump(store.settings_path,{'enabled':True,'content':True,'retentionDays':0,'clearedBefore':0})
        env={'CLAUDE_CONFIG_DIR':'','CODEX_HOME':'','PI_CODING_AGENT_DIR':'','DSH_HOME':''}
        with patch.dict(os.environ,env):store.collect()
        check(not store.progress['errors'],'正式原生采集器无解析错误')
        for case in cases:
            if (case/'gateway-usage.jsonl').exists():
                mapping=json.loads((case/'gateway-connections.json').read_text(encoding='utf-8'))
                store.ingest_gateway(case/'gateway-usage.jsonl',mapping)
        query={'days':'all','pageSize':100}
        data=store.query(query)
        check(data['total']>len(native_ids),'网关保留原生日志缺少的辅助调用，没有按时间或会话强行合并')
        for agent in ('claude','codex','pi','dsh'):
            check(any(r['agent']==agent and r['verified'] and r['gateway']['enabled'] and r['successfulToolResults'] for r in results),agent+' 真实网关调用与文件工具通过')
            rows=[r for r in data['requests'] if r['agent']==agent and r['sourceType']=='gateway+native']
            check(bool(rows),agent+' 原生与网关按响应 ID 合并')
            check(any(r['ttftMs'] is not None for r in rows),agent+' 具有实际首内容等待计时')
        check(any(r['agent']=='codex' and r['verified'] and r['textDeltaEvents'] for r in results),'Codex app-server 经网关有真实文本增量')
        check(any(r['gateway'].get('capturedBodyFiles',0)>0 and r['gateway'].get('bodyFilesAfterDisable')==0 and r['gateway'].get('consentChangedWithoutProcessRestart') for r in results),'额外正文录制实际产生副本，关闭后清空，网关进程未重启')
        check(any(r['agent']=='codex' and r['auth'].get('authMode')=='chatgpt' and r['verified'] for r in results),'Codex 经过网关仍以 ChatGPT 原生凭据调用')
        for agent in ('pi','dsh'):
            native=[r for r in results if r['agent']==agent and r['scenario']=='native']
            check(bool(native),agent+' 原生登录结果有独立记录，不用 API 线路代替登录验证')
        check(all(r['originalFilesUnchanged'] and r['cleanup']['credentialsRemoved'] for r in results),'原始配置指纹未变且凭据副本已清理')
        associated=[r for r in data['requests'] if r['sessionId']]
        check(sum(s['summary']['calls'] for s in data['sessions'])+data['unassociated']==data['summary']['calls'],'概览、对话与未关联请求总数一致')
        check(sum(d['calls'] for d in data['distribution']['agent'])==data['total'],'Agent 分布与请求总数一致')
        for r in associated:
            detail=store.detail(r['id'])
            session=store.session_detail(r['agent'],r['sessionId'],query)
            point=next(p for p in session['session']['points'] if p['id']==r['id'])
            check(point['input']==detail['usage']['context'],'上下文量与请求一致 '+r['id'])
            check(point['cacheRead']==detail['usage']['cacheRead'],'缓存只加一次 '+r['id'])
        check(any(r['contextParts'] for r in data['requests'] if r.get('contextParts')),'实际路由记录提供上下文构成')
        count=data['total']
        with patch.dict(os.environ,env):store.collect()
        for case in cases:
            if (case/'gateway-usage.jsonl').exists():
                store.ingest_gateway(case/'gateway-usage.jsonl',json.loads((case/'gateway-connections.json').read_text()))
        check(store.query(query)['total']==count,'两源重复扫描不增加请求数')
        data=store.query(query)
        dump(out/'observation.json',data)
        report={'capturedAt':time.time(),'checks':checks,'cases':results,'requests':count,
            'nativeIds':len(native_ids),'merged':sum(r['sourceType']=='gateway+native' for r in data['requests']),
            'gatewayOnly':sum(r['sourceType']=='gateway' for r in data['requests']),
            'unassociated':data['unassociated'],'sessions':data['sessionTotal'],'summary':data['summary'],
            'nativeAuthentication':{a:{'passed':any(r['verified'] for r in results if r['agent']==a and r['scenario']=='native'),
                'captures':[r['capture'] for r in results if r['agent']==a and r['scenario']=='native']} for a in ('pi','dsh')},
            'gatewayEngine':sorted({r['gateway'].get('version') for r in results if r['gateway']['enabled']})}
        dump(out/'verification.json',report)
        print(json.dumps({k:v for k,v in report.items() if k not in ('checks','cases')},ensure_ascii=False))
        print('CHECKS',len(checks))
        dump(store.settings_path,{**store.settings(),'enabled':False})
    finally:store.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',action='append',required=True,type=Path)
    parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args()
    verify([p.resolve() for p in args.case],args.out.resolve())
