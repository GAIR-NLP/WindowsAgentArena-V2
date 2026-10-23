import base64
import json
import ntpath
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import guest_mcp_bridge as bridge


class ShellRuntimeTests(unittest.TestCase):
    def session(self, executable=None, expected=None):
        session = bridge.Session.__new__(bridge.Session)
        session.powershell_executable = executable
        session.powershell_expected_version = expected
        session.powershell_runtime = None
        session.lock = threading.Lock()
        session.command_lock = threading.Lock()
        session.failed = False
        session.command = None
        session.shutdown_error = None
        return session

    def test_default_and_explicit_shell_are_probed_and_used_without_path_lookup(self):
        default = r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
        selected = r'C:\waa-bridge\PowerShell-7.6.7\pwsh.exe'
        fake_os = SimpleNamespace(name='nt', path=ntpath, environ={'SystemRoot': r'C:\Windows'})
        for executable, version, edition in [(None, '5.1.26100.5074', 'Desktop'), (selected, '7.6.7', 'Core')]:
            with self.subTest(executable=executable):
                session = self.session(executable, version if executable else None)
                actual = executable or default
                runtime = {'version': version, 'edition': edition, 'executable': actual, 'dotnet': 'runtime'}
                commands = []
                def command(argv, **kwargs):
                    commands.append(argv)
                    result = {'returncode': 0, 'timed_out': False, 'cancelled': False,
                              'output': json.dumps(runtime) if len(commands) == 1 else 'neutral output', 'error': ''}
                    return Mock(wait=Mock(return_value=result))
                with patch.object(bridge, 'os', fake_os), patch.object(bridge, 'Command', side_effect=command):
                    self.assertEqual(session.shell_runtime(), runtime)
                    result = session.powershell("Write-Output 'neutral output'", 10)
                    self.assertEqual(result['runtime'], runtime)
                    self.assertEqual(len(commands), 2)
                    self.assertTrue(all(argv[0] == actual for argv in commands))
                    script = base64.b64decode(commands[1][-1]).decode('utf-16-le')
                    self.assertIn("Write-Output 'neutral output'", script)
                    self.assertNotIn('AppContext', script)
                    self.assertFalse(session.failed)

    def test_runtime_mismatch_stops_before_task_script(self):
        selected = r'C:\waa-bridge\PowerShell-7.6.7\pwsh.exe'
        fake_os = SimpleNamespace(name='nt', path=ntpath, environ={'SystemRoot': r'C:\Windows'})
        for field, value in [('version', '7.5.0'), ('executable', r'C:\other\pwsh.exe'), ('dotnet', '')]:
            session = self.session(selected, '7.6.7')
            runtime = {'version': '7.6.7', 'edition': 'Core', 'executable': selected, 'dotnet': '.NET 10'}
            runtime[field] = value
            result = {'returncode': 0, 'timed_out': False, 'cancelled': False, 'output': json.dumps(runtime)}
            with self.subTest(field=field), patch.object(bridge, 'os', fake_os), \
                 patch.object(bridge, 'Command', return_value=Mock(wait=Mock(return_value=result))) as command:
                with self.assertRaises(bridge.BridgeFailure):
                    session.powershell('throw "must not run"', 10)
                self.assertEqual(command.call_count, 1)
                self.assertIsNone(session.powershell_runtime)

    def test_missing_selected_shell_does_not_fall_back(self):
        session = self.session(r'C:\missing\pwsh.exe', '7.6.7')
        fake_os = SimpleNamespace(name='nt', path=ntpath, environ={'SystemRoot': r'C:\Windows'})
        with patch.object(bridge, 'os', fake_os), patch.object(bridge, 'Command', side_effect=FileNotFoundError) as command:
            with self.assertRaises(FileNotFoundError):
                session.powershell('Write-Output never', 10)
            self.assertEqual(command.call_count, 1)
            self.assertEqual(command.call_args.args[0][0], r'C:\missing\pwsh.exe')

    def test_first_runtime_probe_consumes_the_same_command_budget(self):
        selected = r'C:\candidate\pwsh.exe'
        fake_os = SimpleNamespace(name='nt', path=ntpath, environ={'SystemRoot': r'C:\Windows'})
        runtime = {'version': '7.6.7', 'edition': 'Core', 'executable': selected, 'dotnet': '.NET 10'}
        for elapsed in (4, 12):
            session = self.session(selected, '7.6.7')
            response = {'returncode': 0, 'timed_out': False, 'cancelled': False, 'output': json.dumps(runtime)}
            with self.subTest(elapsed=elapsed), patch.object(bridge, 'os', fake_os), \
                 patch.object(bridge.time, 'monotonic', side_effect=[100, 100 + elapsed]), \
                 patch.object(session, '_run_powershell', return_value=response) as execute:
                if elapsed >= 10:
                    with self.assertRaisesRegex(bridge.BridgeFailure, 'task script was not started'):
                        session.powershell('Write-Output neutral', 10)
                    self.assertEqual(execute.call_count, 1)
                else:
                    session.powershell('Write-Output neutral', 10)
                    self.assertEqual(execute.call_args.args, ('Write-Output neutral', 6))
                self.assertEqual(execute.call_args_list[0].args[1], 10)


if __name__ == '__main__':
    unittest.main()
