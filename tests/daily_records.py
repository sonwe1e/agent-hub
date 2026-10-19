"""Deterministic, sanitized native formats; no synthetic dashboard totals."""
import json
import time
from datetime import datetime, timezone


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat().replace('+00:00','Z')


def write(path, rows, compressed=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = ''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in rows).encode()
    if compressed:
        import zstandard
        raw = zstandard.ZstdCompressor().compress(raw)
    path.write_bytes(raw)
    return path


LONG_MODEL = 'vendor/model-with-a-very-long-name-for-reasoning-and-tools-2026-10-17'
ERROR = '上游返回 429：本线路配额不足。' + '保留错误定位信息与原生证据，不能静默切换未经指定的供应商。' * 18


def seed(home, at=None):
    at = at or time.time()-600
    pi_usage = {'input':18000,'output':420,'cacheRead':3200,'cacheWrite':0,'reasoning':140,
                'cost':{'total':0.025}}
    pi = [{'type':'session','id':'evidence-work','timestamp':iso(at),'cwd':'G:/Projects/evidence-demo'},
          {'type':'message','id':'u1','timestamp':iso(at+1),'message':{'role':'user','content':[{'type':'text','text':'调查失败请求并保留证据。api_key=fixture-secret-do-not-use'}]}},
          {'type':'message','id':'p1','timestamp':iso(at+10),'message':{'role':'assistant','responseId':'pi-success-1','provider':'deepseek','model':LONG_MODEL,'usage':pi_usage,'stopReason':'stop','durationMs':1800,'content':[{'type':'toolCall','id':'t1','name':'read','arguments':{'path':'README.md'}}]}},
          {'type':'message','id':'tool1','timestamp':iso(at+11),'message':{'role':'toolResult','toolCallId':'t1','content':[{'type':'text','text':'文档结果已脱敏'}]}},
          {'type':'message','id':'p2','timestamp':iso(at+20),'message':{'role':'assistant','responseId':'pi-failed-1','provider':'deepseek','model':LONG_MODEL,'usage':{'input':21000,'output':0,'cacheRead':2000,'cacheWrite':0},'stopReason':'error','errorMessage':ERROR}},
          {'type':'compaction','id':'compact-1','timestamp':iso(at+25),'tokensBefore':23000},
          {'type':'message','id':'p3','timestamp':iso(at+30),'message':{'role':'assistant','responseId':'pi-interrupted-1','provider':'unpriced-connection','model':'unknown-priced-model','usage':{'input':6000,'output':30,'cacheRead':0,'cacheWrite':0},'stopReason':'aborted','content':[{'type':'text','text':'流式输出在用户中断后结束。'}]}}]
    paths = [('pi',write(home/'.pi/agent/sessions/project/work.jsonl',pi))]
    claude = [{'type':'user','sessionId':'claude-native-session','uuid':'u1','timestamp':iso(at),'cwd':'G:/Projects/claude-demo','message':{'role':'user','content':'读取文件'}},
              *[{'type':'assistant','sessionId':'claude-native-session','uuid':'c'+str(i),'timestamp':iso(at+40+i),'requestedModel':'opus','message':{'id':'msg-one-response','model':'claude-test-returned-model','usage':{'input_tokens':1400,'output_tokens':n,'cache_read_input_tokens':6000,'cache_creation_input_tokens':100},'stop_reason':'tool_use' if i else None,'content':[{'type':'text','text':'同一回复的多个内容块'}]}} for i,n in enumerate((10,80))]]
    paths.append(('claude',write(home/'.claude/projects/project/main.jsonl',claude)))
    cu = {'input_tokens':10000,'output_tokens':200,'cached_input_tokens':8000,'cache_write_input_tokens':0,'reasoning_output_tokens':50}
    codex = [{'type':'session_meta','timestamp':iso(at),'payload':{'id':'codex-native-session','cwd':'G:/Projects/codex-demo','model_provider':'openai','context_window':240000}},
             {'type':'turn_context','timestamp':iso(at+2),'payload':{'model':'codex-native-model'}},
             {'type':'token_usage_record','timestamp':iso(at+60),'payload':{'thread_id':'codex-native-session','response_id':'resp-native-1','usage':cu}},
             {'type':'event_msg','timestamp':iso(at+61),'payload':{'type':'token_count','info':{'last_token_usage':cu,'total_token_usage':cu,'model_context_window':240000},'rate_limits':{'plan_type':'test-subscription','primary':{'used_percent':37,'window_minutes':300,'resets_at':at+3600}}}}]
    paths.append(('codex',write(home/'.codex/sessions/2026/10/17/rollout.jsonl',codex)))
    dsh = [{'type':'session','version':4,'id':'dsh-native-session','cwd':'G:/Projects/dsh-demo','createdAt':int(at*1000)},
           {'type':'request/context','seq':1,'time':int((at+80)*1000),'data':{'provider':'native-dsh','model':'dsh-requested','contextWindow':128000}},
           {'type':'assistant/message','seq':2,'time':int((at+90)*1000),'data':{'message':{'role':'assistant','content':[],'source':{'provider':'native-dsh','model':'dsh-requested','replayState':{'response':{'responseModel':'dsh-actually-returned','stopReason':'stop'}}}},'usage':{'inputTokens':9000,'outputTokens':110,'cacheReadTokens':1000,'cacheWriteTokens':0}}}]
    folder=home/'.dsh/sessions/project/session-dsh-native-session'
    write(folder/'session.v3.jsonl',dsh)
    paths.append(('dsh',write(folder/'session.v4.jsonl.zstd',dsh,True)))
    return paths
