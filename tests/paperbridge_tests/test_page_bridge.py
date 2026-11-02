"""Exercise the real iframe boundary with a synthetic MCP Apps host."""
import os
from pathlib import Path
import unittest

from paperbridge.page_viewer import page_view_result
import paperbridge.page_viewer as viewer


@unittest.skipUnless(os.environ.get('PAPERBRIDGE_BROWSER_TESTS') == '1', 'Opt-in UI browser test')
class StandardBridgeTests(unittest.TestCase):
    def test_standard_host_outcomes_and_duplicate_protection(self):
        from playwright.sync_api import sync_playwright
        cases = {
            'accepted': 'rpc_accepted', 'rejected': 'rpc_rejected',
            'delivery_error': 'rpc_delivery_error', 'timeout': 'rpc_timeout',
            'invalid': 'rpc_invalid', 'init_timeout': 'rpc_unavailable',
            'init_rejected': 'rpc_unavailable', 'unsupported': 'rpc_unsupported',
            'unadvertised': 'rpc_unsupported', 'spoof': 'rpc_timeout',
            'late': 'rpc_timeout',
            'empty_marker': 'rpc_unsupported', 'legacy_flag': 'rpc_unsupported',
            'legacy_text_flag': 'rpc_unsupported', 'disabled': 'rpc_unsupported',
            'disabled_text': 'rpc_unsupported', 'malformed': 'rpc_invalid_capability',
            'unknown_fields': 'rpc_invalid_capability',
            'text_only': 'rpc_image_unsupported', 'disabled_image': 'rpc_image_unsupported',
            'legacy_image_flag': 'rpc_image_unsupported', 'invalid_image': 'rpc_invalid_capability',
        }
        with sync_playwright() as driver:
            browser = driver.firefox.launch(headless=True)
            try:
                for scenario, expected in cases.items():
                    with self.subTest(scenario=scenario):
                        self.check_host(browser, scenario, expected)
            finally:
                browser.close()

    def check_host(self, browser, scenario, expected):
        # Original tiny PNG, not a publisher page.
        import base64
        png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6lAAAAABJRU5ErkJggg==')
        result = page_view_result(dict(image_bytes=png, id='fixture:1', title='Original fixture',
            url='https://example.org/fixture', page=1, page_count=1), automatic=True)
        page = browser.new_page()
        page.route('**/*', lambda route: route.abort())
        try:
            page.set_content('<iframe id="card"></iframe>')
            page.evaluate('''scenario => {
              window.scenario = scenario; window.messages = []; window.initialized = false;
              window.addEventListener('message', event => {
                const frame = document.getElementById('card').contentWindow;
                if (event.source !== frame || event.data?.jsonrpc !== '2.0') return;
                const request = event.data;
                const reply = data => frame.postMessage({jsonrpc:'2.0', id:request.id, ...data}, '*');
                if (request.method === 'ui/initialize') {
                  window.initializeParams = request.params;
                  if (scenario === 'init_timeout') return;
                  if (scenario === 'init_rejected') { reply({error:{code:-32601, message:'PRIVATE_SENTINEL'}}); return; }
                  const capabilities = scenario === 'unadvertised' ? {} :
                    {message: scenario === 'unsupported' ? {image:{}} : {text:{},image:{}}};
                  if (scenario === 'empty_marker') capabilities.message = {};
                  if (scenario === 'legacy_flag') capabilities.message = true;
                  if (scenario === 'legacy_text_flag') capabilities.message = {text:true};
                  if (scenario === 'disabled') capabilities.message = false;
                  if (scenario === 'disabled_text') capabilities.message = {text:false};
                  if (scenario === 'malformed') capabilities.message = null;
                  if (scenario === 'unknown_fields') capabilities.message = {PRIVATE_SENTINEL:'PRIVATE_SENTINEL'};
                  if (scenario === 'text_only') capabilities.message = {text:{}};
                  if (scenario === 'disabled_image') capabilities.message.image = false;
                  if (scenario === 'legacy_image_flag') capabilities.message.image = true;
                  if (scenario === 'invalid_image') capabilities.message.image = [];
                  reply({result:{protocolVersion:'2026-01-26', hostInfo:{name:'fixture',version:'1'},
                                 hostCapabilities:capabilities}});
                } else if (request.method === 'ui/notifications/initialized') {
                  window.initialized = true;
                } else if (request.method === 'ui/message') {
                  window.messages.push(request);
                  if (scenario === 'timeout' || scenario === 'spoof') return;
                  if (scenario === 'late') { setTimeout(() => reply({result:{}}), 300); return; }
                  if (scenario === 'rejected') reply({error:{code:-32000,message:'PRIVATE_SENTINEL'}});
                  else if (scenario === 'delivery_error') reply({result:{isError:true,detail:'PRIVATE_SENTINEL'}});
                  else if (scenario === 'invalid') reply({result:null});
                  else reply({result:{}});
                }
              });
            }''', scenario)
            frame = page.frames[1]
            frame.evaluate('''meta => {
              const originalTimeout = window.setTimeout.bind(window);
              // Shorten only RPC deadlines; keep ordering and real postMessage delivery.
              window.setTimeout = (fn, ms, ...args) => originalTimeout(fn, ms === 15000 ? 120 : ms, ...args);
              window.uploads = 0; window.compatibilityCalls = 0;
              window.openai = {toolResponseMetadata:meta,
                uploadFile: async () => { window.uploads++; return {fileId:'file_synthetic'}; },
                setWidgetState: state => { window.openai.widgetState = state; },
                sendFollowUpMessage: () => { window.compatibilityCalls++; }
              };
            }''', result.meta)
            html = Path(viewer.__file__).with_name('page_viewer.html').read_text()
            frame.set_content(html)
            if scenario == 'spoof':
                page.wait_for_function('window.messages.length === 1')
                request_id = page.evaluate('window.messages[0].id')
                # Correct ID but untrusted source must not satisfy the pending request.
                frame.evaluate('id => window.postMessage({jsonrpc:"2.0", id, result:{}}, "*")', request_id)
                page.evaluate('''() => document.getElementById('card').contentWindow.postMessage(
                    {jsonrpc:'2.0',id:'wrong-id',result:{}}, '*')''')
            labels = {
                'rpc_accepted': 'Host acknowledged ui/message',
                'rpc_rejected': 'Host rejected ui/message',
                'rpc_delivery_error': 'Host reported message delivery failure',
                'rpc_timeout': 'No host acknowledgement within 15 seconds',
                'rpc_invalid': 'Invalid host acknowledgement',
                'rpc_unavailable': 'Standard bridge unavailable',
                'rpc_unsupported': 'Host does not advertise text message support',
                'rpc_image_unsupported': 'Host does not advertise image message support',
                'rpc_invalid_capability': 'Unrecognized message capability format',
            }
            expected_text = 'Automatic: ' + labels[expected]
            frame.wait_for_function('text => document.getElementById("continuation-result").textContent.includes(text)', arg=expected_text)
            self.assertEqual(frame.evaluate('window.uploads'), 1)
            self.assertEqual(frame.evaluate('window.compatibilityCalls'), 0)
            count = 0 if expected in ('rpc_unavailable', 'rpc_unsupported', 'rpc_image_unsupported', 'rpc_invalid_capability') else 1
            self.assertEqual(page.evaluate('window.messages.length'), count)
            self.assertNotIn('PRIVATE_SENTINEL', frame.locator('body').text_content())
            self.assertIn('Capability shape:', frame.locator('#continuation-result').text_content())
            if scenario == 'empty_marker':
                self.assertIn('message=empty_object', frame.locator('#continuation-result').text_content())
                self.assertIn('unspecified_empty_marker', frame.locator('#continuation-result').text_content())
            self.assertEqual(frame.evaluate('window.openai.widgetState.imageIds'), ['file_synthetic'])
            if count:
                self.assertTrue(page.evaluate('window.initialized'))
                params = page.evaluate('window.messages[0].params')
                self.assertEqual(params['role'], 'user')
                self.assertEqual(params['content'][0]['type'], 'text')
                self.assertIn('Do not call papers_view_page again', params['content'][0]['text'])
                self.assertIn('1. https://example.org/fixture; PDF page 1.', params['content'][0]['text'])
                self.assertEqual(params['content'][1:], [dict(type='image', mimeType='image/png',
                    data=base64.b64encode(png).decode())])
                self.assertNotIn(base64.b64encode(png).decode(), params['content'][0]['text'])
                self.assertNotIn(base64.b64encode(png).decode(), str(frame.evaluate('window.openai.widgetState')))
            if scenario == 'rejected':
                self.assertIn('Automatic error code: -32000.', frame.locator('#continuation-result').text_content())
            if scenario == 'accepted':
                # One deliberate manual retry uses the existing attachment, not the alias.
                frame.get_by_role('button', name='Continue with this page').click()
                frame.wait_for_function('document.getElementById("continuation-result").textContent.includes("After-button: Host acknowledged ui/message")')
                count += 1
                frame.evaluate('document.getElementById("continue").dispatchEvent(new Event("click"))')
                self.assertEqual(page.evaluate('window.messages[1].params.content'), params['content'])
            if scenario == 'late':
                page.wait_for_timeout(400)
                self.assertIn(expected_text, frame.locator('#continuation-result').text_content())
            # Repeated notifications and remount must not resurrect a send.
            frame.evaluate('window.dispatchEvent(new Event("openai:set_globals")); window.dispatchEvent(new Event("pagehide"))')
            frame.set_content(html)
            frame.get_by_role('button', name='Page uploaded').wait_for()
            page.wait_for_timeout(180)
            self.assertEqual(page.evaluate('window.messages.length'), count)
            self.assertEqual(frame.evaluate('window.uploads'), 1)
            self.assertEqual(frame.evaluate('window.compatibilityCalls'), 0)
        finally:
            page.close()
