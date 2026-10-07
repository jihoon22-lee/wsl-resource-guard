"""Read-only disk UI checks, including mocked capacity/failure presentation."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

URL=os.environ['WRG_TEST_BASE_URL']


def main():
    with sync_playwright() as p:
        cached=sorted((Path.home()/'.cache/ms-playwright').glob('chromium-*/chrome-linux64/chrome'))
        browser=p.chromium.launch(executable_path=str(cached[-1]) if cached else None)
        page=browser.new_page(viewport={'width':1440,'height':1080})
        errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(URL+'/#disks')
        expect(page.locator('#view-disks .windows-disks .disk-row')).to_have_count(3)
        expect(page.locator('.vhd-info')).to_contain_text('ext4.vhdx')
        expect(page.locator('.host-space')).to_contain_text('E:')
        expect(page.locator('#view-disks')).to_contain_text('20% 미만 Warning')
        Path('artifacts').mkdir(exist_ok=True)
        page.screenshot(path='artifacts/disks-desktop.png',full_page=True)
        actual=page.request.get(URL+'/api/disks').json()
        assert len(actual['history'])>=1
        first=actual['disks'][0]
        first.update(available_percent=19,available_bytes=first['total_bytes']*.19,
                     used_percent=81,used_bytes=first['total_bytes']*.81,severity='warning')
        second=actual['disks'][1]
        second.update(available_percent=9,available_bytes=second['total_bytes']*.09,
                      used_percent=91,used_bytes=second['total_bytes']*.91,severity='critical')
        actual['disks'][2].update(status='unmounted',error='드라이브 미연결')
        page.route('**/api/disks*',lambda route:route.fulfill(json=actual))
        page.locator('#refresh').click()
        expect(page.locator('#refresh')).to_be_enabled()
        expect(page.locator('.windows-disks .disk-meter.warning')).to_have_count(1)
        expect(page.locator('.windows-disks .disk-meter.critical')).to_have_count(1)
        expect(page.locator('#view-disks')).to_contain_text('마지막 확인값 표시')
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.unroute('**/api/disks*')
        page.locator('#refresh').click()
        expect(page.locator('#refresh')).to_be_enabled()
        page.screenshot(path='artifacts/disks-mobile.png',full_page=True)
        page.locator('[data-view="overview"]').click()
        expect(page.locator('#disk-overview .disk-row')).to_have_count(4)
        assert not errors,errors
        browser.close()
    print('Disk UI passed: four volumes, VHD/host relation, daily history, warning/critical/unmounted fixtures, mobile layout')


if __name__=='__main__':main()
