"""Edge journey for M35 browser-only submitter intake."""

import json
import os
from pathlib import Path
import unittest
from urllib.parse import urlsplit

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:
    expect = sync_playwright = None


EDGE = Path(os.environ.get('GUANYE_TEST_EDGE', r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'))
PUBLIC = Path(__file__).resolve().parent / 'public'
ORIGIN = 'http://127.0.0.1:8765'
WORKSPACE = '11111111-1111-4111-8111-111111111111'
OWN_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
OTHER_ID = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'


def intake_rows():
    return [
        {
            'id': OWN_ID, 'workspace_id': WORKSPACE, 'submitted_by': 'alice', 'title': '我的旧投递',
            'original_name': 'old.pdf', 'object_path': WORKSPACE + '/intake/' + OWN_ID + '/old.pdf',
            'content_type': 'application/pdf', 'size_bytes': 12, 'sha256': 'a' * 64,
            'ai_allowed': False, 'status': 'completed', 'created_at': '2026-11-22T01:00:00Z',
        },
        {
            'id': OTHER_ID, 'workspace_id': WORKSPACE, 'submitted_by': 'bob', 'title': 'Bob 私密投递',
            'original_name': 'other.pdf', 'object_path': WORKSPACE + '/intake/' + OTHER_ID + '/other.pdf',
            'content_type': 'application/pdf', 'size_bytes': 9, 'sha256': 'b' * 64,
            'ai_allowed': True, 'status': 'queued', 'created_at': '2026-11-22T02:00:00Z',
        },
    ]


def supabase_mock(role):
    rows = json.dumps(intake_rows(), ensure_ascii=False)
    return """
window.__intake={sequence:[],uploads:[],inserts:[],removes:[],queries:[],rpcCalls:[],rows:%s,failStorage:false,failInsert:false};
window.supabase={createClient:()=>({
 auth:{
  getSession:async()=>({data:{session:{access_token:'fixture-token',user:{id:'alice',email:'alice@example.test'}}}}),
  refreshSession:async()=>({data:{session:{access_token:'refreshed-token',user:{id:'alice'}}}}),
  signOut:async()=>({data:null}),
  updateUser:async()=>({data:{user:{id:'alice'}}})
 },
 rpc:async(name,args)=>{
  window.__intake.rpcCalls.push({name,args});
  if(name==='single_judge_access')return {data:{mode:'single-judge',role:%s}};
  if(name==='get_review_library')return {data:{decks:[],entries:[]}};
  if(name==='get_review_state')return {data:{revision:0,configRevision:0,maxScore:5,dimensions:[],reviews:{}}};
  if(name==='review_score_summary')return {data:{}};
  return {error:{message:'unexpected rpc '+name,code:'UNEXPECTED'}};
 },
 storage:{from(bucket){return {
  upload:async(path,bytes,options)=>{
   window.__intake.sequence.push('upload');
   window.__intake.uploads.push({path,size:bytes.byteLength,options});
   if(window.__intake.failStorage)return {data:null,error:{message:'Storage 直传失败',code:'STORAGE'}};
   return {data:{path},error:null};
  },
  remove:async paths=>{
   window.__intake.removes.push([...paths]);
   return {data:[],error:null};
  },
  createSignedUrls:async paths=>({data:paths.map(path=>({path,signedUrl:'/signed/'+path})),error:null})
 };}},
 from(table){
  if(table!=='single_judge_intake')throw new Error('unexpected table '+table);
  let filters=[],selected='',order=null,limitValue=null;
  const query={
   select(columns){selected=columns;return query;},
   insert(row){
    window.__intake.sequence.push('insert');
    window.__intake.inserts.push(JSON.parse(JSON.stringify(row)));
    window.__intake.queries.push({operation:'insert',filters:[],selected:'',order:null,limit:null});
    if(window.__intake.failInsert)return Promise.resolve({data:null,error:{message:'表登记失败',code:'RLS'}});
    return Promise.resolve({data:null,error:null});
   },
   eq(key,value){filters.push([key,value]);return query;},
   order(column,options){order={column,options};return query;},
   limit(value){limitValue=value;return query;},
   then(resolve,reject){
    window.__intake.queries.push({operation:'select',filters:[...filters],selected,order,limit:limitValue});
    const data=window.__intake.rows.filter(row=>filters.every(([key,value])=>row[key]===value)).slice(0,limitValue||window.__intake.rows.length);
    return Promise.resolve({data,error:null}).then(resolve,reject);
   }
  };
  return query;
 }
})};
""" % (rows, json.dumps(role))


@unittest.skipUnless(sync_playwright and EDGE.is_file(), 'requires Playwright and Microsoft Edge')
class SingleJudgeIntakeBrowserTest(unittest.TestCase):
    def open_app(self, browser, role, local_import):
        page = browser.new_page(viewport={'width': 1360, 'height': 1100})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        config = {
            'url': 'https://review.test', 'anonKey': 'fixture-key', 'workspaceId': WORKSPACE,
            'singleJudge': True, 'localImport': local_import,
            'aiGatewayUrl': 'https://gateway.example/ai',
        }

        def static_asset(path):
            relative = path.lstrip('/')
            asset = (PUBLIC / relative).resolve()
            return asset if PUBLIC in asset.parents and asset.is_file() else None

        def respond(route):
            url = urlsplit(route.request.url)
            if url.hostname != '127.0.0.1' or url.port != 8765:
                route.abort()
                return
            if url.path == '/':
                route.fulfill(path=str(PUBLIC / 'index.html'))
                return
            if url.path == '/cloud-config.js':
                route.fulfill(content_type='text/javascript; charset=utf-8', body='window.CLOUD_CONFIG=' + json.dumps(config))
                return
            if url.path == '/vendor/supabase.js':
                route.fulfill(content_type='text/javascript; charset=utf-8', body=supabase_mock(role))
                return
            asset = static_asset(url.path)
            if asset:
                route.fulfill(path=str(asset))
                return
            route.abort()

        page.route('**/*', respond)
        page.goto(ORIGIN + '/', wait_until='domcontentloaded')
        return page, errors

    def test_submitter_uploads_to_receipt_and_storage_failure_is_visible(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=str(EDGE))
            try:
                page, errors = self.open_app(browser, 'submitter', False)
                expect(page.locator('#web-intake-form')).to_be_visible()
                expect(page.locator('.sidebar')).to_contain_text('网页投递')
                expect(page.locator('.sidebar')).not_to_contain_text('正式评分工作台')
                expect(page.locator('[data-web-intake-receipts]')).to_contain_text('我的旧投递')
                expect(page.locator('[data-web-intake-receipts]')).not_to_contain_text('Bob 私密投递')
                rpc_names = page.evaluate('window.__intake.rpcCalls.map(item => item.name)')
                self.assertNotIn('get_review_library', rpc_names)

                page.fill('#web-intake-title', '浏览器投递')
                page.check('#web-intake-ai')
                page.set_input_files('#web-intake-file', {
                    'name': 'valid.pdf', 'mimeType': 'application/pdf', 'buffer': b'hello',
                })
                page.click('#web-intake-submit')
                page.wait_for_function('window.__intake.inserts.length === 1')
                expect(page.locator('#web-intake-progress')).to_contain_text('投递成功')
                expect(page.locator('#web-intake-result')).to_contain_text('浏览器投递')

                state = page.evaluate('window.__intake')
                self.assertEqual(state['sequence'], ['upload', 'insert'])
                self.assertEqual(len(state['uploads']), 1)
                upload = state['uploads'][0]
                insert = state['inserts'][0]
                self.assertRegex(upload['path'], r'^' + WORKSPACE + r'/intake/[0-9a-f-]{36}/valid\.pdf$')
                self.assertEqual(upload['options'], {'contentType': 'application/pdf', 'upsert': False})
                self.assertEqual(insert['submitted_by'], 'alice')
                self.assertEqual(insert['workspace_id'], WORKSPACE)
                self.assertEqual(insert['object_path'], upload['path'])
                self.assertEqual(insert['content_type'], 'application/pdf')
                self.assertEqual(insert['size_bytes'], 5)
                self.assertEqual(insert['sha256'], '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824')
                self.assertEqual(insert['ai_allowed'], True)
                self.assertEqual(insert['status'], 'queued')
                last_select = next(item for item in reversed(state['queries']) if item['operation'] == 'select')
                self.assertEqual(last_select['filters'], [['workspace_id', WORKSPACE], ['submitted_by', 'alice']])

                page.click('button[data-action="web-intake-restart"]')
                page.evaluate('window.__intake.failStorage = true')
                page.set_input_files('#web-intake-file', {
                    'name': 'retry.pdf', 'mimeType': 'application/pdf', 'buffer': b'retry',
                })
                page.click('#web-intake-submit')
                expect(page.locator('#web-intake-error')).to_contain_text('Storage 直传失败')
                expect(page.locator('#web-intake-error')).to_contain_text('重试')
                expect(page.locator('#web-intake-result')).not_to_be_visible()
                self.assertEqual(page.evaluate('window.__intake.inserts.length'), 1)
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_judge_keeps_web_entry_and_local_professional_import(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=str(EDGE))
            try:
                page, errors = self.open_app(browser, 'judge', True)
                expect(page.locator('.sidebar')).to_contain_text('网页投递')
                expect(page.locator('.sidebar')).to_contain_text('本机专业投递')
                page.click('button[data-action="web-intake"]')
                expect(page.locator('#web-intake-form')).to_be_visible()
                page.locator('button[data-action="close-modal"]').first.click()
                page.click('button[data-action="professional-import"]')
                expect(page.locator('#professional-import-form')).to_be_visible()
                self.assertEqual(errors, [])
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
