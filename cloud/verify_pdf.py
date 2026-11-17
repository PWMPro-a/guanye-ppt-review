"""Import a disposable two-page PDF on the deployed admin frontend, then clean it up."""
import json
from pathlib import Path
import urllib.request
import uuid
import fitz
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1]
def main():
    config=json.loads((ROOT/'cloud/private-deployment.json').read_text(encoding='utf-8'))
    account=next(a for a in json.loads((ROOT/'cloud/private-accounts.json').read_text(encoding='utf-8')) if a['role']=='admin')
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
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True,channel='msedge',proxy={'server':'http://127.0.0.1:7897'})
            page=browser.new_page()
            page.on('pageerror',lambda e:print('PDF page error:',str(e),flush=True))
            page.goto('https://pwmpro-a.github.io/guanye-ppt-review/',wait_until='domcontentloaded',timeout=60000)
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
            browser.close()
        print('PASS: deployed admin PDF conversion, two pages in order, private upload and catalog insertion')
    finally:
        rows=request('/rest/v1/decks?select=id,data&data->>name=eq.'+name)
        for row in rows:
            request('/storage/v1/object/ppt-library',{'prefixes':[i['path'] for i in row['data']['images']]},'DELETE')
            request('/rest/v1/decks?id=eq.'+row['id'],method='DELETE')

if __name__=='__main__':main()
