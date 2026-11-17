"""Import a disposable two-page PDF as an ordinary reviewer, then clean it up."""
import json
import hashlib
import secrets
from pathlib import Path
import urllib.request
import uuid
import fitz
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1]
def main():
    config=json.loads((ROOT/'cloud/private-deployment.json').read_text(encoding='utf-8'))
    account={'email':'browser-test-'+uuid.uuid4().hex+'@example.com','password':secrets.token_urlsafe(24)}
    workspace=str(uuid.uuid4())
    invitation=secrets.token_urlsafe(24)
    user_id=None
    name='pdf-verification-'+uuid.uuid4().hex
    pdf=ROOT/'.test-output/online-import-test.pdf'
    doc=fitz.open()
    for label in ['PAGE ONE','PAGE TWO']:
        page=doc.new_page(width=640,height=360);page.insert_text((60,120),label,fontsize=32)
    doc.save(pdf);doc.close()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({'https':'http://127.0.0.1:7897'}))
    def request(path,body=None,method=None):
        req=urllib.request.Request(config['url']+path,data=None if body is None else json.dumps(body).encode(),method=method or ('GET' if body is None else 'POST'),headers={'apikey':config['serviceKey'],'Authorization':'Bearer '+config['serviceKey'],'Content-Type':'application/json'})
        with opener.open(req,timeout=45) as r:
            raw=r.read();return json.loads(raw) if raw else None
    try:
        request('/rest/v1/workspaces',{'id':workspace,'name':'Browser test','owner_email':'none@example.test','invite_hash':hashlib.sha256(invitation.encode()).hexdigest()})
        user_id=request('/auth/v1/admin/users',{**account,'email_confirm':True})['id']
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,channel='msedge',proxy={'server':'http://127.0.0.1:7897'})
            page=browser.new_page()
            public_config={'url':config['url'],'anonKey':config['anonKey'],'workspaceId':workspace}
            page.route('**/cloud-config.js',lambda route:route.fulfill(content_type='application/javascript',body='window.CLOUD_CONFIG='+json.dumps(public_config)+';'))
            page.on('pageerror',lambda e:print('PDF page error:',str(e),flush=True))
            page.goto('https://pwmpro-a.github.io/guanye-ppt-review/',wait_until='domcontentloaded',timeout=60000)
            page.locator('#cloud-name').fill('Temporary reviewer');page.locator('#cloud-invitation').fill(invitation)
            page.locator('#cloud-email').fill(account['email']);page.locator('#cloud-password').fill(account['password']);page.locator('#cloud-auth button[type="submit"]').click()
            page.locator('[data-action="import"]').click(timeout=60000)
            page.locator('#document-files').set_input_files(str(pdf));page.locator('#import-name').fill(name)
            page.locator('#import-submit').click()
            try:expect(page.locator('#modal')).not_to_be_visible(timeout=90000)
            except Exception:
                print('PDF status:',page.locator('#selected-files').inner_text(),page.locator('#import-error').inner_text(),flush=True)
                page.screenshot(path=str(ROOT/'.test-output/cloud-pdf-debug.png'),full_page=True)
                raise
            expect(page.locator('.slide-position')).to_contain_text('02')
            rows=request('/rest/v1/decks?select=id,data&data->>name=eq.'+name)
            assert len(rows)==1 and len(rows[0]['data']['images'])==2
            assert rows[0]['data']['images'][0]['name']=='0001.jpg'
            page.locator('[data-scoring-mode="page"]').click()
            page.locator('#score-visual').fill('3');page.locator('#score-visual').press('Tab')
            page.locator('#score-layout').fill('4');page.locator('#score-layout').press('Tab')
            page.locator('#review-note').fill('First page live note')
            page.locator('[data-action="next-page"]').click()
            expect(page.locator('#score-visual')).to_have_value('')
            page.locator('#score-visual').fill('5');page.locator('#score-visual').press('Tab')
            page.locator('#score-layout').fill('4');page.locator('#score-layout').press('Tab')
            page.wait_for_function('!pending && !saving')
            page.reload()
            expect(page.locator('[data-scoring-mode="page"]')).to_have_class('selected')
            expect(page.locator('#score-visual')).to_have_value('3')
            expect(page.locator('#review-note')).to_have_value('First page live note')
            page.locator('.topbar [data-cloud-action="team"]').click()
            expect(page.locator('.team-mean')).to_have_text('4')
            expect(page.locator('.team-table details')).to_be_visible()
            page.locator('[data-action="close-modal"]').last.click()
            page.locator('[data-scoring-mode="deck"]').click()
            expect(page.locator('#score-visual')).to_have_value('')
            page.wait_for_function('!pending && !saving')
            browser.close()
        print('PASS: deployed reviewer PDF import, private upload, page scoring/notes persistence, team averages and mode switching')
    finally:
        rows=request('/rest/v1/decks?select=id,data&data->>name=eq.'+name)
        for row in rows:
            request('/storage/v1/object/ppt-library',{'prefixes':[i['path'] for i in row['data']['images']]},'DELETE')
            request('/rest/v1/reviews?deck_id=eq.'+row['id'],method='DELETE')
            request('/rest/v1/decks?id=eq.'+row['id'],method='DELETE')
        request('/rest/v1/workspaces?id=eq.'+workspace,method='DELETE')
        if user_id:request('/auth/v1/admin/users/'+user_id,method='DELETE')

if __name__=='__main__':main()
