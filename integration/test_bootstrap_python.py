"""Execute generated guest startup code with bounded host-side Windows fakes."""
import ctypes
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import mock_open, patch

import vm


class BootstrapPythonTests(unittest.TestCase):
    def generated_code(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'official.zip').touch()
            (root / 'hcu.zip').touch()
            integration = root / 'integration'
            integration.mkdir()
            (integration / 'guest_mcp_bridge.py').write_text('pass')
            with patch.object(vm, 'ROOT', root), patch.object(vm, 'REPO', root), \
                 patch.object(vm, 'DRIVER_INSTALL', ''), patch.object(vm, 'upload'), \
                 patch.object(vm, 'deploy_activation_worker', return_value={}), \
                 patch.object(vm, 'token_path', return_value=root / 'token'), \
                 patch.object(vm, 'execute', return_value={'returncode': 0}) as execute:
                vm.bootstrap('unused-controller', root, root / 'hcu.zip', root / 'official.zip')
            return execute.call_args.args[1]

    def test_startup_uses_current_python_and_only_child_path(self):
        code = self.generated_code()
        for original in ({'PATH': r'C:\Program Files\GIMP 2\bin;C:\Windows', 'OTHER': 'preserved'},
                         {'OTHER': 'preserved'}):
            with self.subTest(original=original):
                python = r'C:\Users\Docker\AppData\Local\Programs\Python\Python310\python.exe'
                kernel = SimpleNamespace(ProcessIdToSessionId=lambda pid, pointer: setattr(pointer._obj, 'value', 1))
                with patch.dict(os.environ, original, clear=True), \
                     patch.object(sys, 'executable', python), \
                     patch('pathlib.Path', PureWindowsPath), \
                     patch.object(ctypes, 'windll', SimpleNamespace(kernel32=kernel), create=True), \
                     patch.object(subprocess, 'CREATE_NO_WINDOW', 0x08000000, create=True), \
                     patch.object(subprocess, 'run') as run, \
                     patch.object(subprocess, 'Popen', return_value=SimpleNamespace(pid=123)) as popen, \
                     patch('builtins.open', mock_open()), patch('builtins.print'):
                    exec(compile(code, '<generated-guest-bootstrap>', 'exec'), {})
                    self.assertEqual(dict(os.environ), original)
                    argv = popen.call_args.args[0]
                    env = popen.call_args.kwargs['env']
                    self.assertEqual(argv[0], python)
                    self.assertEqual(argv[1:3], ['-u', r'C:\waa-bridge\guest_mcp_bridge.py'])
                    self.assertEqual(env['PATH'], str(PureWindowsPath(python).parent) + ';' +
                                     str(PureWindowsPath(python).parent / 'Scripts') + ';' + original.get('PATH', ''))
                    self.assertEqual(env['OTHER'], original['OTHER'])
                    self.assertIsNot(env, os.environ)
                    # The only other subprocess is the pre-existing firewall command.
                    self.assertEqual(run.call_args.args[0][0], 'netsh')


if __name__ == '__main__':
    unittest.main()
