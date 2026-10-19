"""Isolated UI transaction receipts; never write live files."""
from support import engine
from test_api import fake_push_all
plans={}
def preview(h,only=None,*,reconcile=False):
    pid=f'fixture-{len(plans)+1}'
    plans[pid]=list(only or [])
    return {'planId':pid,'targets':plans[pid],'warnings':[],'files':[{'path':str(engine.HOME/'test-clients'/f'{n}.json'),'action':'update'} for n in plans[pid]]}
def apply(h,pid):
    if pid not in plans:raise ValueError('预览已失效')
    return {'transactionId':pid,'results':fake_push_all(h,plans.pop(pid))}
def restore(h,pid):
    return {'transactionId':pid,'restored':[],'created':[],'reverted':True}
