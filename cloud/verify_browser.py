"""Headless browser smoke test of the real deployed website, using a seeded reviewer."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
SITE='https://pwmpro-a.github.io/guanye-ppt-review/'

def main():
    accounts=json.loads((ROOT/'cloud/private-accounts.json').read_text(encoding='utf-8'))
    account=next(a for a in accounts if a['email']=='reviewer2@guanye.local')
    errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,channel='msedge',proxy={'server':'http://127.0.0.1:7897'})
        page=browser.new_page(viewport={'width':1440,'height':1000})
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(SITE,wait_until='domcontentloaded',timeout=60000)
        page.locator('#cloud-email').fill(account['email'])
        page.locator('#cloud-password').fill(account['password'])
        page.locator('#cloud-auth button[type="submit"]').click()
        expect(page.locator('.deck-card').first).to_be_visible(timeout=60000)
        page.wait_for_function('document.querySelector(".deck-card img")?.naturalWidth>0',timeout=30000)
        expect(page.locator('[data-action="import"]')).to_be_hidden()
        expect(page.locator('.topbar [data-cloud-action="team"]')).to_be_visible()
        page.locator('.deck-card').first.click()
        expect(page.locator('#score-visual')).to_be_visible()
        expect(page.locator('[data-action="append"]')).to_be_hidden()
        page.locator('.topbar [data-cloud-action="team"]').click()
        expect(page.locator('.team-table tbody tr').first).to_be_visible(timeout=30000)
        page.screenshot(path=str(ROOT/'.test-output/cloud-team-live.png'),full_page=True)
        page.locator('[data-action="close-modal"]').last.click()
        page.locator('[data-action="back"]').click()
        page.screenshot(path=str(ROOT/'.test-output/cloud-library-live.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        expect(page.locator('.deck-card').first).to_be_visible()
        page.locator('.avatar[data-cloud-action="account"]').click()
        expect(page.locator('[data-cloud-action="password"]').last).to_be_visible()
        page.locator('[data-action="close-modal"]').last.click()
        page.screenshot(path=str(ROOT/'.test-output/cloud-mobile-live.png'),full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'), 'Mobile horizontal overflow'
        assert not errors,errors
        browser.close()
    print('PASS: deployed sign-in, private library images, reviewer controls, team summary, 390px layout, no JavaScript errors')

if __name__=='__main__':main()
