"""Bounded selection, permission isolation and actual iframe upload sequencing."""
import base64
import os
from pathlib import Path
import unittest
from unittest.mock import Mock

from paperbridge.page_viewer import pages_view_result
from paperbridge.service import PaperBridge
import paperbridge.page_viewer as viewer

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6lAAAAABJRU5ErkJggg==')


def fixture(index):
    return dict(image_bytes=PNG, id='pb:' + str(index)*64 + ':0',
                title='Original drawing ' + str(index), url='https://example.org/paper/' + str(index),
                page=index, page_count=4)


class PageBatchTests(unittest.TestCase):
    def bridge(self):
        bridge = PaperBridge.__new__(PaperBridge)
        bridge.get_page = Mock()
        return bridge

    def test_invalid_complete_selection_never_starts_retrieval(self):
        bridge = self.bridge()
        for selection in ([], [{}], [{'reference': 'x', 'page': True}],
                          [{'reference': 'x', 'page': 1}, {'reference': 'x', 'page': 0}]):
            with self.assertRaises(ValueError):
                bridge.get_pages(selection)
        bridge.get_page.assert_not_called()

    def test_five_and_six_pages_are_valid_when_images_fit_the_byte_budget(self):
        for count in (5, 6):
            with self.subTest(count=count):
                bridge = self.bridge()
                pages = [dict(fixture(index), page_count=count) for index in range(1, count + 1)]
                bridge.get_page.side_effect = pages
                selection = [{'reference': page['id'], 'page': page['page']} for page in pages]
                result = bridge.get_pages(selection, max_edge=1600)
                self.assertEqual(bridge.get_page.call_count, count)
                self.assertEqual(result['prepared_count'], count)
                self.assertEqual(result['unprocessed_count'], 0)
                self.assertEqual(result['image_bytes_total'], count * len(PNG))
                self.assertEqual([item['selection_index'] for item in result['selection_coverage']], list(range(count)))
                self.assertTrue(all(item['status'] == 'image_prepared' for item in result['selection_coverage']))
                wrapped = pages_view_result(result, max_edge=1600)
                self.assertEqual([item['id'] for item in wrapped.structuredContent['pages']], [page['id'] for page in pages])
                self.assertTrue(all(item['widget_image_fetch_max_edge'] == 1600 for item in wrapped.structuredContent['pages']))

    def test_oversized_next_page_stops_retrieval_and_preserves_remaining_selection(self):
        bridge = self.bridge()
        large = PNG + b'x' * (4_100_000 - len(PNG))
        bridge.get_page.side_effect = [dict(fixture(i), image_bytes=large) for i in (1, 2)]
        selection = [{'reference': f'10.1234/source-{i}', 'page': i} for i in range(1, 7)]
        result = bridge.get_pages(selection)
        self.assertEqual(bridge.get_page.call_count, 2)
        self.assertEqual(result['prepared_count'], 1)
        self.assertEqual(result['unprocessed_count'], 4)
        self.assertEqual(result['image_byte_limit'], 8_000_000)
        self.assertEqual([item['status'] for item in result['selection_coverage']],
                         ['image_prepared', 'page_image_unavailable'] + ['not_processed'] * 4)
        for index, entry in enumerate(result['selection_coverage'][1:], start=1):
            self.assertEqual(entry['reason'], 'batch_byte_limit')
            self.assertEqual(entry['reference'], selection[index]['reference'])
            self.assertEqual(entry['page'], selection[index]['page'])
            self.assertEqual(entry['selection_index'], index)

    def test_exactly_full_byte_budget_does_not_render_the_next_page(self):
        bridge = self.bridge()
        exact = dict(fixture(1), image_bytes=PNG + b'x' * (8_000_000 - len(PNG)))
        bridge.get_page.return_value = exact
        selection = [{'reference': f'10.1234/source-{i}', 'page': i} for i in range(1, 7)]
        result = bridge.get_pages(selection)
        self.assertEqual(bridge.get_page.call_count, 1)
        self.assertEqual(result['image_bytes_total'], 8_000_000)
        self.assertEqual(result['unprocessed_count'], 5)
        self.assertEqual(len(result['selection_coverage']), 6)
        self.assertTrue(all(item['status'] == 'not_processed' and item['reason'] == 'batch_byte_limit'
                            for item in result['selection_coverage'][1:]))

    def test_partial_permission_failure_and_canonical_duplicate(self):
        bridge = self.bridge()
        alias = dict(fixture(1), id='pb:' + '1'*64 + ':7')
        bridge.get_page.side_effect = [fixture(1), PermissionError('PRIVATE'), alias, fixture(2)]
        result = bridge.get_pages([{'reference': str(i), 'page': 1} for i in range(4)])
        self.assertEqual(result['prepared_count'], 2)
        self.assertEqual([item['status'] for item in result['selection_coverage']],
                         ['image_prepared', 'external_model_permission_required', 'duplicate_page', 'image_prepared'])
        wrapped = pages_view_result(result, automatic=True)
        self.assertTrue(wrapped.meta['paperbridge/page-fetch'])
        self.assertNotIn('paperbridge/images', wrapped.meta)
        self.assertNotIn('paperbridge/image', wrapped.meta)
        self.assertEqual([page['url'] for page in wrapped.structuredContent['pages']],
                         ['https://example.org/paper/1', 'https://example.org/paper/2'])
        self.assertNotIn('PRIVATE', wrapped.content[0].text)
        self.assertNotIn('image_bytes', wrapped.content[0].text.replace('image_bytes_total', 'bytes_total'))
        self.assertFalse(wrapped.structuredContent['entire_papers_read'])
        self.assertEqual(wrapped.structuredContent['continuation_delay_seconds'], 0)

    def test_total_byte_limit_and_all_unavailable(self):
        bridge = self.bridge()
        large = dict(fixture(1), image_bytes=PNG + b'x'*4_100_000)
        bridge.get_page.side_effect = [large, dict(large, id='pb:' + '2'*64 + ':0')]
        result = bridge.get_pages([{'reference': 'a', 'page': 1}, {'reference': 'b', 'page': 1}])
        self.assertEqual(result['prepared_count'], 1)
        self.assertEqual(result['selection_coverage'][1]['reason'], 'batch_byte_limit')
        bridge.get_page.side_effect = [PermissionError()]
        result = bridge.get_pages([{'reference': 'a', 'page': 1}])
        self.assertIs(pages_view_result(result), result)
        self.assertEqual(result['status'], 'page_images_unavailable')

    @unittest.skipUnless(os.environ.get('PAPERBRIDGE_BROWSER_TESTS') == '1', 'Opt-in UI browser test')
    def test_batch_iframe_delivery_partial_retry_and_remount(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as driver:
            browser = driver.firefox.launch(headless=True)
            try:
                for fail_second in (False, True):
                    with self.subTest(fail_second=fail_second):
                        self.check_iframe(browser, fail_second)
            finally:
                browser.close()

    def check_iframe(self, browser, fail_second):
        response = pages_view_result(dict(pages=[fixture(1), fixture(2)],
            selection_coverage=[], requested_count=2, prepared_count=2), automatic=True)
        html = Path(viewer.__file__).with_name('page_viewer.html').read_text()
        page = browser.new_page()
        page.route('**/*', lambda route: route.abort())
        try:
            page.set_content('<iframe id="card"></iframe>')
            page.evaluate("""() => {
              window.messages = [];
              window.addEventListener('message', event => {
                const child = document.getElementById('card').contentWindow;
                if (event.source !== child || event.data?.jsonrpc !== '2.0') return;
                const msg = event.data;
                const reply = result => child.postMessage({jsonrpc:'2.0',id:msg.id,result}, '*');
                if (msg.method === 'ui/initialize') reply({protocolVersion:'2026-01-26',
                  hostInfo:{name:'test', version:'1'},hostCapabilities:{message:{text:{},image:{}}}});
                if (msg.method === 'ui/message') { window.messages.push(msg); reply({}); }
              });
            }""")
            frame = page.frames[1]
            frame.evaluate("""({meta, failSecond, png}) => {
              window.uploads = []; window.savedStates = []; window.failed = false; window.reads = [];
              window.openai = {toolResponseMetadata:meta,
                callTool: async (name, args) => {
                  window.reads.push(args.page);
                  const info = meta['paperbridge/page'].pages.find(item => item.id === args.reference && item.page === args.page);
                  if (name !== 'papers_get_page' || !info || args.max_edge !== info.widget_image_fetch_max_edge)
                    throw new Error('Unexpected page request');
                  return {content:[{type:'text', text:JSON.stringify(info)}, {type:'image', mimeType:'image/png', data:png}]};
                },
                uploadFile: async file => {
                  window.uploads.push(file.name);
                  if (failSecond && window.uploads.length === 2 && !window.failed) {
                    window.failed = true; throw new Error('PRIVATE');
                  }
                  return {fileId:'file_' + file.name};
                },
                setWidgetState: state => { window.openai.widgetState = state; window.savedStates.push(state); }
              };
            }""", {'meta':response.meta, 'failSecond':fail_second, 'png':base64.b64encode(PNG).decode()})
            frame.set_content(html)
            if fail_second:
                frame.wait_for_function('window.failed && document.getElementById("status").textContent.includes("could not")')
                self.assertEqual(page.evaluate('window.messages.length'), 0)
                state = frame.evaluate('window.openai.widgetState')
                self.assertEqual(state['imageIds'], [])
                self.assertEqual(len(state['privateContent']['paperbridge']['fileIds']), 1)
                frame.set_content(html)  # Preserve saved host state across the remount.
                frame.get_by_role('button', name='Add pages to chat').click()
                frame.get_by_role('button', name='Pages uploaded').wait_for()
                frame.get_by_role('button', name='Continue with these pages').click()
            page.wait_for_function('window.messages.length === 1')
            frame.wait_for_function('text => document.getElementById("continuation-result").textContent.includes(text)',
                                    arg=('After-button: ' if fail_second else 'Automatic: ') + 'Host acknowledged ui/message')
            state = frame.evaluate('window.openai.widgetState')
            self.assertEqual(len(state['imageIds']), 2)
            self.assertIsInstance(state['modelContent'], str)
            self.assertEqual(state['modelContent'].splitlines()[1:3], [
                '1. https://example.org/paper/1; PDF page 1.',
                '2. https://example.org/paper/2; PDF page 2.'])
            self.assertEqual(state['privateContent']['paperbridge']['fileIds'], state['imageIds'])
            self.assertTrue(all(file_id not in state['modelContent'] for file_id in state['imageIds']))
            uploads = frame.evaluate('window.uploads')
            self.assertEqual(len(uploads), 3 if fail_second else 2)
            self.assertEqual(uploads.count('paperbridge-selection-1-page-1.png'), 1)
            self.assertEqual(frame.evaluate('window.reads'), [1, 2, 2, 1] if fail_second else [1, 2])
            self.assertEqual(frame.locator('img').count(), 2)
            reads = frame.evaluate('window.reads')
            frame.set_content(html)
            frame.wait_for_timeout(100)
            self.assertEqual(page.evaluate('window.messages.length'), 1)
            self.assertEqual(frame.evaluate('window.uploads'), uploads)
            self.assertEqual(frame.evaluate('window.reads'), reads)
            self.assertNotIn('PRIVATE', frame.locator('body').text_content())
        finally:
            page.close()
