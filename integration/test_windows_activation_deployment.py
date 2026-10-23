"""Deployment refuses drift before replacing the worker selected by the baseline."""
from pathlib import Path
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from windows_activation_deployment import deploy, python_processes


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = self.root / 'server'
        self.server.mkdir()
        self.source = self.root / 'staged.py'
        self.target = self.server / 'windows_window_activation.py'
        self.source.write_text("CHANGE_ID='new'\nINSTALL_MARKER='stable-hook'\n")
        self.target.write_text("CHANGE_ID='old'\n")
        (self.server / 'main.py').write_text('# stable-hook\nfrom windows_window_activation import install as _waa_install_activation\n_waa_install_activation(app)\n')

    def test_atomic_replacement_reads_back_revision_and_keeps_controller(self):
        main_before = (self.server / 'main.py').read_text()
        result = deploy(self.source, self.server, 'new', [])
        self.assertEqual(result['worker_change_id'], 'new')
        self.assertEqual(result['previous_worker_change_id'], 'old')
        self.assertEqual(result['controller_reload'], 'expected_on_source_replacement')
        self.assertEqual((self.server / 'main.py').read_text(), main_before)
        self.assertTrue(self.source.exists())

    def test_replacement_is_staged_on_target_volume_and_failure_preserves_old_worker(self):
        replace = os.replace
        def same_directory_replace(source, target):
            self.assertEqual(Path(source).parent, Path(target).parent)
            replace(source, target)
        with patch('windows_activation_deployment.os.replace', side_effect=same_directory_replace):
            deploy(self.source, self.server, 'new', [])
        self.assertEqual(list(self.server.glob('.activation-*')), [])
        self.target.write_text("CHANGE_ID='old'\n")
        with patch('windows_activation_deployment.os.replace', side_effect=PermissionError('blocked')):
            with self.assertRaises(PermissionError):
                deploy(self.source, self.server, 'new', [])
        self.assertIn("'old'", self.target.read_text())
        self.assertEqual(list(self.server.glob('.activation-*')), [])

    def test_unknown_revision_preserves_previous_worker(self):
        with self.assertRaisesRegex(RuntimeError, 'revision'):
            deploy(self.source, self.server, 'unexpected', [])
        self.assertIn("'old'", self.target.read_text())
        self.assertTrue(self.source.exists())

    def test_missing_and_duplicate_hook_refuse_replacement(self):
        for text in ['', '# stable-hook\n# stable-hook\n']:
            with self.subTest(text=text):
                (self.server / 'main.py').write_text(text)
                with self.assertRaisesRegex(RuntimeError, 'registration'):
                    deploy(self.source, self.server, 'new', [])
                self.assertIn("'old'", self.target.read_text())

    def test_active_worker_refuses_replacement(self):
        process = SimpleNamespace(info={'pid': 1, 'cmdline': ['python', str(self.target), '--worker']})
        with self.assertRaisesRegex(RuntimeError, 'running'):
            deploy(self.source, self.server, 'new', [process])
        self.assertIn("'old'", self.target.read_text())

    def test_unrelated_process_and_module_mentions_do_not_block(self):
        rows = [SimpleNamespace(info={'cmdline': ['python', '-c', "print('windows_window_activation.py --worker')"]}),
                SimpleNamespace(info={'cmdline': ['python', 'unrelated.py', '--worker']})]
        self.assertEqual(deploy(self.source, self.server, 'new', rows)['worker_change_id'], 'new')

    def test_invalid_staged_source_preserves_previous_worker(self):
        self.source.write_text('def broken(')
        with self.assertRaises(SyntaxError):
            deploy(self.source, self.server, 'new', [])
        self.assertIn("'old'", self.target.read_text())

    def test_builtin_process_inventory_parses_scalar_and_array_results(self):
        import json
        row = {'ProcessId': 77, 'CommandLine': 'python fixture.py'}
        for response in [row, [row]]:
            with patch('windows_activation_deployment.subprocess.check_output', return_value=json.dumps(response)), \
                 patch('windows_activation_deployment.parse_windows_command_line', return_value=['python', 'fixture.py']):
                result = python_processes()
                self.assertEqual(result[0].info, {'pid': 77, 'cmdline': ['python', 'fixture.py']})

    def test_unreadable_process_inventory_fails_closed(self):
        with patch('windows_activation_deployment.subprocess.check_output', return_value='[{"ProcessId":77,"CommandLine":null}]'):
            with self.assertRaisesRegex(RuntimeError, 'inspect'):
                python_processes()


if __name__ == '__main__':
    unittest.main()
