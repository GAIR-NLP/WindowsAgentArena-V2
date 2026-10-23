"""Offline controller checks; no guest, browser, model, or evaluator is started."""
import json
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

CLIENT = Path(__file__).resolve().parents[1] / 'src/win-arena-container/client'
sys.path.insert(0, str(CLIENT))

import requests
from desktop_env.controllers import setup


def response(status=200, body=None, text=None):
    value = requests.Response()
    value.status_code = status
    value._content = (text if text is not None else json.dumps(body)).encode()
    return value


class SetupControllerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache = Path(directory.name)
        self.controller = setup.SetupController('test-guest', str(self.cache))
        sleep = patch.object(setup.time, 'sleep')
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_http_and_transport_failures_stop_later_steps(self):
        parameters = {
            'open': {'path': 'input.docx'},
            'launch': {'command': ['application']},
            'activate_window': {'window_name': 'Document'},
            'close_window': {'window_name': 'Document'},
            'close_all': {},
            'create_folder': {'path': 'folder'},
            'create_file': {'path': 'output.txt'},
            'recycle_file': {'path': 'output.txt'},
            'change_wallpaper': {'path': 'wallpaper.png'},
        }
        for name, params in parameters.items():
            for outcome in [response(404, text='not found'), response(500, text='server failed'),
                            response(body={'status': 'error', 'message': 'failed'}),
                            requests.ConnectionError('disconnected')]:
                with self.subTest(method=name, outcome=outcome), \
                     patch.object(setup.requests, 'post', side_effect=[outcome]) as post:
                    with self.assertRaises((requests.RequestException, RuntimeError)):
                        self.controller.setup([
                            {'type': name, 'parameters': params},
                            {'type': 'launch', 'parameters': {'command': ['must-not-start']}},
                        ])
                    self.assertEqual(post.call_count, 1)

    def test_plain_text_success_is_accepted(self):
        with patch.object(setup.requests, 'post', return_value=response(text='Window activated')):
            self.controller._activate_window_setup('Document')

    def test_window_readiness_retries_only_missing_window_then_ready(self):
        missing = response(404, text='Window Document not found (empty results)')
        ready = response(text='Window activated successfully')
        with patch.object(setup.requests, 'post', side_effect=[missing, missing, ready]) as post:
            self.controller._activate_window_setup('Document', strict=True, by_class=False)
        self.assertEqual(post.call_count, 3)
        for call in post.call_args_list:
            self.assertEqual(json.loads(call.kwargs['data']),
                             {'window_name': 'Document', 'strict': True, 'by_class': False})
            self.assertGreater(call.kwargs['timeout'], 0)
            self.assertLessEqual(call.kwargs['timeout'], 5)

    def test_window_readiness_deadline_bounds_requests_and_stops_next_step(self):
        clock = [0.0]
        def advance(seconds):
            clock[0] += seconds
        def send(*args, **kwargs):
            self.assertLessEqual(kwargs['timeout'], 30.0 - clock[0])
            return response(404, text='Window Document not found (empty results)')
        with patch.object(setup.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(setup.time, 'sleep', side_effect=advance), \
             patch.object(setup.requests, 'post', side_effect=send) as post:
            with self.assertRaisesRegex(RuntimeError, 'readiness deadline exceeded'):
                self.controller.setup([
                    {'type': 'activate_window', 'parameters': {'window_name': 'Document'}},
                    {'type': 'launch', 'parameters': {'command': ['must-not-start']}},
                ])
        self.assertEqual(clock[0], 30)
        self.assertEqual(post.call_count, 60)

    def test_window_readiness_does_not_retry_other_errors_or_unknown_input(self):
        outcomes = [response(404, text='Window Other not found (empty results)'),
                    response(404, text='Window Document not found (strict mode).'),
                    response(404, text='proxy not found'),
                    response(400, text='Window Document not found (empty results)'),
                    response(500, text='Window Document not found (empty results)'),
                    requests.Timeout('unknown activation outcome')]
        for outcome in outcomes:
            with self.subTest(outcome=outcome), patch.object(setup.requests, 'post', side_effect=[outcome]) as post:
                with self.assertRaises(requests.RequestException):
                    self.controller._activate_window_setup('Document')
                self.assertEqual(post.call_count, 1)

    def test_window_readiness_does_not_accept_response_after_deadline(self):
        clock = [0.0]
        def send(*args, **kwargs):
            clock[0] = 31.0
            return response(text='Window activated successfully')
        with patch.object(setup.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(setup.requests, 'post', side_effect=send) as post:
            with self.assertRaisesRegex(RuntimeError, 'last HTTP 200'):
                self.controller._activate_window_setup('Document')
        self.assertEqual(post.call_count, 1)

    def test_upload_and_cached_download_propagate_http_errors(self):
        source = self.cache / 'input.txt'
        source.write_text('input')
        url = 'https://example.invalid/input'
        cached = self.cache / f'{uuid.uuid5(uuid.NAMESPACE_URL, url)}_input.txt'
        cached.write_text('cached input')
        cases = [('_upload_file_setup', [{'local_path': str(source), 'path': 'input.txt'}]),
                 ('_download_setup', [{'url': url, 'path': 'input.txt'}])]
        for name, files in cases:
            with self.subTest(method=name), patch.object(setup.requests, 'post', return_value=response(500)):
                with self.assertRaises(requests.HTTPError):
                    getattr(self.controller, name)(files)

    def test_missing_upload_file_raises_before_request(self):
        with patch.object(setup.requests, 'post') as post:
            with self.assertRaises(FileNotFoundError):
                self.controller._upload_file_setup([{'local_path': str(self.cache / 'missing'), 'path': 'target'}])
            post.assert_not_called()

    def test_download_exhaustion_preserves_request_error(self):
        failure = requests.ConnectionError('download disconnected')
        with patch.object(setup.requests, 'get', side_effect=failure) as get:
            with self.assertRaises(requests.RequestException) as caught:
                self.controller._download_setup([{'url': 'https://example.invalid/file', 'path': 'file'}])
            self.assertEqual(get.call_count, 3)
            self.assertIs(caught.exception.__cause__, failure)

    def test_execute_failure_stops_and_does_not_replay(self):
        for outcome in [response(500), requests.Timeout('unknown input outcome'),
                        response(body={'returncode': 2, 'output': '', 'error': 'bad command'}),
                        response(body={'status': 'error', 'returncode': 0}),
                        response(body={}), response(body={'returncode': True}),
                        response(text='invalid JSON')]:
            with self.subTest(outcome=outcome), patch.object(setup.requests, 'post', side_effect=[outcome]) as post:
                with self.assertRaises((requests.RequestException, RuntimeError)):
                    self.controller.setup([
                        {'type': 'execute', 'parameters': {'command': ['command']}},
                        {'type': 'launch', 'parameters': {'command': ['must-not-start']}},
                    ])
                self.assertEqual(post.call_count, 1)

    def test_execute_preserves_stderr_and_output_for_success(self):
        body = {'returncode': 0, 'output': 'result', 'error': 'warning only'}
        with patch.object(setup.requests, 'post', return_value=response(body=body)):
            self.controller._execute_setup(['command'], stdout='stdout.txt', stderr='stderr.txt')
        self.assertEqual((self.cache / 'stdout.txt').read_text(), 'result')
        self.assertEqual((self.cache / 'stderr.txt').read_text(), 'warning only')

    def test_until_keeps_nonzero_and_output_condition_semantics(self):
        for condition in [{'returncode': 7}, {'stdout': 'ready'}, {'stderr': 'ready'},
                          {'returncode': 0, 'stdout': 'ready'}]:
            body = {'returncode': 7, 'output': 'ready', 'error': 'ready'}
            with self.subTest(condition=condition), patch.object(setup.requests, 'post', return_value=response(body=body)) as post:
                self.controller._execute_setup(['probe'], until=condition)
                self.assertEqual(post.call_count, 1)

    def test_until_retries_nonmatching_condition_then_completes(self):
        responses = [response(body={'returncode': 1, 'output': '', 'error': ''}),
                     response(body={'returncode': 0, 'output': '', 'error': ''})]
        with patch.object(setup.requests, 'post', side_effect=responses) as post:
            self.controller._execute_setup(['probe'], until={'returncode': 0})
            self.assertEqual(post.call_count, 2)

    def test_until_http_retry_exhaustion_raises(self):
        with patch.object(setup.requests, 'post', return_value=response(503)) as post:
            with self.assertRaises(requests.HTTPError):
                self.controller._execute_setup(['probe'], until={'returncode': 0})
            self.assertEqual(post.call_count, 5)

    def test_until_request_retry_can_recover(self):
        responses = [requests.ConnectionError('temporary'),
                     response(body={'returncode': 0, 'output': '', 'error': ''})]
        with patch.object(setup.requests, 'post', side_effect=responses) as post:
            self.controller._execute_setup(['probe'], until={'returncode': 0})
            self.assertEqual(post.call_count, 2)

    def test_chrome_connection_exhaustion_raises_after_last_attempt(self):
        context = Mock()
        failure = RuntimeError('CDP unavailable')
        context.chromium.connect_over_cdp.side_effect = failure
        with patch.object(setup, 'sync_playwright') as playwright:
            playwright.return_value.__enter__.return_value = context
            with self.assertRaisesRegex(RuntimeError, 'CDP unavailable'):
                self.controller._chrome_open_tabs_setup(['https://example.invalid'])
        self.assertEqual(context.chromium.connect_over_cdp.call_count, 15)

    def test_chrome_navigation_warning_is_not_setup_failure(self):
        browser = Mock()
        context = Mock()
        default_page = Mock()
        context.pages = [default_page]
        browser.contexts = [context]
        context.new_page.return_value.goto.side_effect = setup.TimeoutError('navigation timeout')
        with patch.object(setup, 'sync_playwright') as playwright, self.assertLogs(setup.logger, logging.WARNING) as logged:
            playwright.return_value.__enter__.return_value.chromium.connect_over_cdp.return_value = browser
            returned = self.controller._chrome_open_tabs_setup(['https://example.invalid'])
        self.assertEqual(returned, (browser, context))
        self.assertIn('exceeds time limit', logged.output[0])

    def test_setup_import_and_nonbrowser_dispatch_do_not_import_evaluators(self):
        script = '''
import importlib.abc, sys
class RejectEvaluators(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.startswith('desktop_env.evaluators'):
            raise AssertionError('Unexpected evaluator import: ' + fullname)
sys.meta_path.insert(0, RejectEvaluators())
from unittest.mock import patch
from desktop_env.controllers.setup import SetupController
controller = SetupController('test-guest', '.')
with patch('desktop_env.controllers.setup.time.sleep'):
    controller.setup([{'type': 'sleep', 'parameters': {'seconds': 0}}])
assert not any(name.startswith('desktop_env.evaluators') for name in sys.modules)
'''
        completed = subprocess.run([sys.executable, '-c', script], cwd=CLIENT,
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == '__main__':
    unittest.main()
