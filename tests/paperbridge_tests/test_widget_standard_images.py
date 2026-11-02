"""Explicit image messages with a standard host and no legacy widget APIs."""
import base64
import json
import os
from pathlib import Path
import unittest

from paperbridge import page_viewer as viewer
from paperbridge_tests.test_page_batch import fixture, PNG


@unittest.skipUnless(os.environ.get('PAPERBRIDGE_BROWSER_TESTS') == '1', 'Opt-in UI browser test')
class StandardImageMessageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.driver = sync_playwright().start()
        try:
            cls.browser = cls.driver.chromium.launch(headless=True)
        except BaseException:
            cls.driver.stop()
            raise
        cls.html = Path(viewer.__file__).with_name('page_viewer.html').read_text()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.driver.stop()

    def widget(self, *, deferred=False, image_support=True, storage=True, full=False, reject=False, automatic=True):
        if deferred:
            result = viewer.pages_view_result(dict(pages=[fixture(2), fixture(1)], requested_count=2), automatic=automatic)
        else:
            result = viewer.page_view_result(fixture(1), automatic=automatic)
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.route('**/*', lambda route: route.abort())
        page.route('https://widget-fixture.test/', lambda route: route.fulfill(
            content_type='text/html', body='<iframe id="card"></iframe>'))
        page.goto('https://widget-fixture.test/')
        page.evaluate('''({meta,png,imageSupport,reject}) => {
          window.messages = []; window.reads = [];
          window.addEventListener('message', event => {
            const child = document.getElementById('card').contentWindow;
            if (event.source !== child || event.data?.jsonrpc !== '2.0') return;
            const msg = event.data;
            const reply = result => child.postMessage({jsonrpc:'2.0',id:msg.id,result}, '*');
            if (msg.method === 'ui/initialize') reply({protocolVersion:'2026-01-26',
              hostInfo:{name:'original-standard-fixture',version:'1'},
              hostCapabilities:{message:{text:{}, ...(imageSupport ? {image:{}} : {})},serverTools:{}}});
            if (msg.method === 'ui/notifications/initialized') child.postMessage({jsonrpc:'2.0',
              method:'ui/notifications/tool-result',params:{_meta:meta}}, '*');
            if (msg.method === 'tools/call') {
              window.reads.push(msg.params);
              const info = meta['paperbridge/page'].pages.find(p => p.id === msg.params.arguments.reference);
              reply({content:[{type:'text',text:JSON.stringify(info)}, {type:'image',mimeType:'image/png',data:png}]});
            }
            if (msg.method === 'ui/message') {
              window.messages.push(msg);
              if (reject) child.postMessage({jsonrpc:'2.0',id:msg.id,
                error:{code:-32000,message:'PRIVATE_SENTINEL'}}, '*');
              else reply({});
            }
          });
        }''', dict(meta=result.meta, png=base64.b64encode(PNG).decode(), imageSupport=image_support, reject=reject))
        frame = page.frames[1]
        if not storage:
            frame.evaluate('''Object.defineProperty(window, 'sessionStorage', {
              get() { throw new Error('PRIVATE_SENTINEL'); }
            })''')
        elif full:
            frame.evaluate('''() => { for (let i=0;i<64;i++)
              sessionStorage.setItem('paperbridge:page-message:' + i, '{}'); }''')
        frame.set_content(self.html)
        return page, frame

    def test_inline_and_deferred_messages_attach_pixels_without_legacy_apis(self):
        for deferred in (False, True):
            with self.subTest(deferred=deferred):
                page, frame = self.widget(deferred=deferred)
                page.wait_for_function('window.messages.length === 1')
                frame.wait_for_function("document.getElementById('continuation-result').textContent.includes('Host acknowledged ui/message')")
                self.assertTrue(frame.evaluate('window.openai === undefined'))
                content = page.evaluate('window.messages[0].params.content')
                count = 2 if deferred else 1
                self.assertEqual(content[1:], [dict(type='image',mimeType='image/png',
                    data=base64.b64encode(PNG).decode())] * count)
                self.assertNotIn(base64.b64encode(PNG).decode(), content[0]['text'])
                guards = frame.evaluate('Object.values(sessionStorage)')
                self.assertEqual(len(guards), 1)
                self.assertNotIn(base64.b64encode(PNG).decode(), json.dumps(guards))
                self.assertNotIn('Selected PDF pages', guards[0])
                self.assertTrue(json.loads(guards[0])['followUpAttempted'])
                self.assertEqual(page.evaluate('window.reads.length'), count if deferred else 0)
                frame.evaluate("window.dispatchEvent(new Event('pagehide'))")
                frame.set_content(self.html)
                frame.get_by_role('button', name='Page images prepared').wait_for()
                self.assertEqual(page.evaluate('window.messages.length'), 1)
                self.assertEqual(page.evaluate('window.reads.length'), count if deferred else 0)
                frame.get_by_role('button', name='Continue with these pages' if deferred else 'Continue with this page').click()
                page.wait_for_function('window.messages.length === 2')
                self.assertEqual(page.evaluate('window.messages[1].params.content'), content)
                frame.evaluate("document.getElementById('continue').dispatchEvent(new Event('click')); "
                               "window.dispatchEvent(new Event('pagehide'))")
                frame.set_content(self.html)
                frame.get_by_role('button', name='Page images prepared').wait_for()
                self.assertTrue(frame.locator('#continue').is_disabled())
                self.assertEqual(page.evaluate('window.messages.length'), 2)

    def test_missing_image_capability_or_attempt_storage_never_sends(self):
        for options in ({'image_support': False}, {'storage': False}, {'full': True}):
            with self.subTest(options=options):
                page, frame = self.widget(**options)
                if options.get('image_support') is False:
                    frame.wait_for_function("document.getElementById('status').textContent.includes('does not expose')")
                    self.assertTrue(frame.locator('#attach').is_disabled())
                elif options.get('full'):
                    frame.wait_for_function("document.getElementById('status').textContent.includes('limit of 64')")
                    self.assertIn('No message was sent', frame.locator('#status').inner_text())
                    self.assertIn('do not expire during the session', frame.locator('#status').inner_text())
                    self.assertIn('per app origin', frame.locator('#guard-help').inner_text())
                    self.assertIn('Do not clear these records while delivery may still be pending',
                                  frame.locator('#status').inner_text())
                    self.assertEqual(frame.evaluate('Object.values(sessionStorage)'), ['{}'] * 64)
                    frame.get_by_role('button', name='Add page to chat').click()
                    self.assertEqual(frame.evaluate('Object.values(sessionStorage)'), ['{}'] * 64)
                else:
                    frame.wait_for_function("document.getElementById('status').textContent.includes('could not')")
                self.assertEqual(page.evaluate('window.messages.length'), 0)
                self.assertEqual(page.evaluate('window.reads.length'), 0)
                self.assertNotIn('PRIVATE_SENTINEL', frame.locator('body').inner_text())

    def test_manual_standard_only_guidance_matches_actual_image_message(self):
        page, frame = self.widget(automatic=False)
        frame.get_by_role('button', name='Add page to chat').click()
        frame.get_by_role('button', name='Page images prepared').wait_for()
        self.assertIn('Use Continue with this page once to send the selected image blocks',
                      frame.locator('#status').inner_text())
        self.assertEqual(page.evaluate('window.messages.length'), 0)
        frame.get_by_role('button', name='Continue with this page').click()
        page.wait_for_function('window.messages.length === 1')
        self.assertEqual(page.evaluate('window.messages[0].params.content.slice(1)'),
                         [dict(type='image', mimeType='image/png', data=base64.b64encode(PNG).decode())])

    def test_rejected_native_message_never_repeats_after_remount(self):
        page, frame = self.widget(reject=True)
        frame.wait_for_function("document.getElementById('continuation-result').textContent.includes('Host rejected ui/message')")
        self.assertNotIn('PRIVATE_SENTINEL', frame.locator('body').inner_text())
        frame.evaluate("window.dispatchEvent(new Event('pagehide'))")
        frame.set_content(self.html)
        frame.get_by_role('button', name='Page images prepared').wait_for()
        self.assertEqual(page.evaluate('window.messages.length'), 1)
