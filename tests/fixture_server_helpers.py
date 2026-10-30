"""Isolated UI transaction receipts; never write live files."""
from support import engine
from test_api import fake_push_all
plans={}
def preview(h,only=None,*,reconcile=False,model_sync=None):
    pid=f'fixture-{len(plans)+1}'
    plans[pid]={'targets':list(only or []),'model_sync':model_sync}
    warnings = ['OpenCode 同步会用 Hub 的供应商清单替换 opencode.json 的 provider 段'] if 'opencode' in plans[pid]['targets'] else []
    return {'planId':pid,'targets':plans[pid]['targets'],'warnings':warnings,'files':[{'path':str(engine.HOME/'test-clients'/f'{n}.json'),'action':'update'} for n in plans[pid]['targets']]}
def apply(h,pid):
    if pid not in plans:raise ValueError('预览已失效')
    plan=plans.pop(pid)
    if plan['model_sync'] is not None:
        h.cfg['modelSync']=plan['model_sync']
        engine.save_json(engine.HUB_CONFIG,h.cfg)
    return {'transactionId':pid,'results':fake_push_all(h,plan['targets'])}
def restore(h,pid):
    return {'transactionId':pid,'restored':[],'created':[],'reverted':True}
