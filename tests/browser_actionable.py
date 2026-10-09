"""Decision-oriented mobile journeys. Fixture traffic only; never signal a real session."""
import argparse
import copy
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit,parse_qs

from playwright.sync_api import sync_playwright,expect
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from browser_offline import ORIGIN,serve,fixtures,SESSION_PID,session_row,target_fixture

INCIDENT='a'*32
TARGET=f'{SESSION_PID:032x}'
TOOL=f'{SESSION_PID+1:032x}'


def data():
    store=fixtures()
    shared=target_fixture(TARGET,store)
    shared.update(provider='codex',name='codex app-server',kind='shared_runtime',killable=False,kill_block_reason='여러 작업의 공용 실행기입니다. 원래 앱에서 작업별로 중지하세요.',
                  projects=[{'project':'alpha'},{'project':'beta'}],impact='alpha와 beta의 작업이 함께 중단될 수 있습니다.',
                  tasks=[],identity_note='대화 연결 미확인 · 프로젝트로 대화를 추정하지 않습니다.',
                  tools=[{'id':TOOL,'name':'browser-tool','project':'alpha','rss_kib':10000,'process_count':2}])
    tool=target_fixture(TOOL,store)
    tool.update(kind='tool',name='browser-tool',project='alpha',projects=[{'project':'alpha'}],
                tasks=[{'thread_id':'fixture-explicit-id','title':'문서 검토'}],identity_note='명시적 작업 ID와 제목 연결')
    row={'id':INCIDENT,'state':'active','revision':1,'severity':'warning','priority':'review',
         'reason':'MCP 도구 메모리가 기준을 넘었습니다','guidance':'현재 점유 상태와 작업 필요성을 확인하세요.',
         'opened_at':time.time()-7200,'observed_at':time.time(),'evidence':{'available_gib':8,'psi_some_avg60':0,'swap_out_mib_per_minute':0},
         'targets':[shared],'decision':{},'delivery':{'destinations':{
            'one':{'label':'Samsung Internet','status':'retry','attempts':1,'detail':'전송 실패','attempted_at':time.time()},
            'two':{'label':'Gmail','status':'accepted','attempts':1,'detail':'접수됨','attempted_at':time.time()}}}}
    store['/api/incidents']={'version':1,'updated_at':time.time(),'incidents':[row],'stale':False}
    store['/api/targets/'+TARGET]=shared;store['/api/targets/'+TOOL]=tool
    return store,row


def run_case(browser,width,mode):
    state,row=data();posts=[];gets=[];errors=[];attempts=[]
    context=browser.new_context(viewport={'width':width,'height':844},is_mobile=width<768,has_touch=width<768,service_workers='block')
    context.add_init_script("localStorage.setItem('wrg-refresh','0')")
    page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
    def route(r):
        path=urlsplit(r.request.url).path
        if path=='/api/incidents' and parse_qs(urlsplit(r.request.url).query).get('id'):
            if mode=='expired-link':return r.fulfill(status=404,json={'error':'경보 기록이 없거나 보관 기간이 지났습니다.'})
            return r.fulfill(json={'incident':row,'stale':mode=='stale','updated_at':time.time()})
        if path=='/api/actions/preview':
            payload=r.request.post_data_json;posts.append((path,payload));identifier=payload['target_id']
            operation={'id':identifier,'target':state['/api/targets/'+identifier], 'status':'preview',
                       'expires_at':time.time()+120,'message':'영향을 확인하세요.','signalled':[],'remaining':[]}
            state['/api/actions/'+identifier]=operation
            return r.fulfill(json=operation)
        if path.endswith('/execute'):
            attempts.append(r.request.post_data_json)
            operation=state['/api/actions/'+TOOL]
            if mode=='scope-changed':operation.update(status='rejected',message='영향 범위가 변경돼 실행하지 않았습니다.')
            else:operation.update(status='remaining',message='자식 프로세스가 아직 실행 중입니다.',signalled=[SESSION_PID+1],remaining=[SESSION_PID+2])
            if mode=='lost-response':return r.abort('failed')
            return r.fulfill(json=operation)
        return serve(r,posts,gets,state)
    page.route(ORIGIN+'/**',route)
    try:
        page.goto(ORIGIN+'/#incident?id='+INCIDENT)
        if mode=='expired-link':
            expect(page.locator('[data-section="incidentDetail"]')).to_contain_text('보관 기간')
            assert '#incident?id='+INCIDENT in page.url
            assert not attempts
        else:
            expect(page.locator('#incident-detail')).to_contain_text('Samsung Internet')
            expect(page.locator('#incident-detail')).to_contain_text('Gmail')
            expect(page.locator('#incident-detail')).to_contain_text('실패 · 재시도')
            if mode=='stale':
                expect(page.locator('#incident-detail')).to_contain_text('최근 상태 미확인')
                expect(page.locator('[data-incident-choice]')).to_have_count(0)
            else:
                page.locator(f'a[href="#target?id={TARGET}"]').click()
                expect(page.locator('#target-detail')).to_contain_text('alpha와 beta')
                expect(page.locator('#preview-termination')).to_have_count(0)
                page.locator(f'a[href="#target?id={TOOL}"]').click()
                expect(page.locator('#target-detail')).to_contain_text('문서 검토')
                page.locator('#preview-termination').click()
                expect(page.locator('#execute-termination')).to_be_disabled()
                if mode=='cancel':
                    page.get_by_role('link',name='취소 · 작업으로 돌아가기').click()
                    assert not attempts
                else:
                    page.locator('#understand-impact').check()
                    page.locator('#execute-termination').click()
                    if mode=='lost-response':
                        expect(page.locator('#operation-detail')).to_contain_text('종료를 다시 보내지 않고')
                        page.locator('#check-operation').click()
                    expect(page.locator('#operation-detail')).to_contain_text('영향 범위가 변경' if mode=='scope-changed' else '자식 프로세스가 아직')
                    page.reload()
                    expect(page.locator('#operation-detail')).to_contain_text('영향 범위가 변경' if mode=='scope-changed' else '자식 프로세스가 아직')
                    assert len(attempts)==1,attempts
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth'),(width,mode)
        assert not errors,errors
        print(f'PASS {width}px {mode}',flush=True)
    finally:context.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--browser',choices=['chromium','webkit'],default='chromium');args=parser.parse_args()
    with sync_playwright() as p:
        browser=getattr(p,args.browser).launch()
        try:
            for width in (1440,390):
                for mode in ('cancel','partial','lost-response','scope-changed','stale','expired-link'):
                    run_case(browser,width,mode)
        finally:browser.close()

if __name__=='__main__':main()
