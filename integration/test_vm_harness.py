import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import patch

import vm


class VMTests(unittest.TestCase):
    def test_cua_archive_requires_explicit_existing_package(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / 'official package with spaces.zip'
            package.touch()
            with self.assertRaisesRegex(ValueError, 'explicit'):
                vm.resolve_cua_archive()
            self.assertEqual(vm.resolve_cua_archive(package), package.resolve())
            with self.assertRaisesRegex(FileNotFoundError, 'CUA archive is missing'):
                vm.resolve_cua_archive(root / 'missing.zip')

    def test_explicit_cua_archive_is_uploaded_without_official_discovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            candidate = root / 'candidate.zip'
            candidate.touch()
            integration = root / 'integration'
            integration.mkdir()
            (integration / 'guest_mcp_bridge.py').write_text('pass')
            (root / 'hcu.zip').touch()
            config = vm.load_guest_driver_config()
            config['arms']['cua']['env'] = {
                'CUA_DRIVER_RS_HOME': r'C:\waa-bridge\cua-perception-profile',
                'CUA_DRIVER_PERCEPTION_CATALOG': r'C:\waa-bridge\perception\catalog.json'}
            config['arms']['hcu']['env'] = {'HCU_TEST_SETTING': 'retained'}
            config_path = root / 'developer-arms.json'
            config_path.write_text(json.dumps(config))
            with patch.object(vm, 'ROOT', root), patch.object(vm, 'REPO', root), \
                 patch.object(vm, 'execute', return_value={}), patch.object(vm, 'upload') as upload, \
                 patch.object(vm, 'deploy_activation_worker', return_value={}) as deploy, \
                 patch.object(vm, 'token_path', return_value=root / 'token'):
                returned = vm.bootstrap('unused', root, root / 'hcu.zip', candidate, config_path)
            self.assertEqual(returned, config)
            self.assertEqual(json.loads((root / 'arms.json').read_text()), config)
            cua_uploads = [call.args[1] for call in upload.call_args_list
                           if call.args[2] == r'C:\waa-bridge\cua.zip']
            self.assertEqual(cua_uploads, [candidate.resolve()])
            deploy.assert_not_called()

    def test_missing_bridge_source_stops_before_copy_or_vm_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot = root / 'snapshot'
            snapshot.mkdir()
            (snapshot / 'data.img').write_bytes(b'fixture disk')
            archive = root / 'driver.zip'
            archive.touch()
            args = argparse.Namespace(run_id='missing-bridge', arm='hcu', snapshot=snapshot,
                                      hcu_archive=archive, cua_archive=archive, hyperv='N', lock_network=False,
                                      image='windows-test:1', network='test-network', bridge_script=root / 'missing.py')
            with patch.object(vm, 'ROOT', root), patch.object(vm, 'command') as command:
                with self.assertRaisesRegex(FileNotFoundError, 'Bridge script is missing'):
                    vm.start(args)
                command.assert_not_called()

    def test_token_initialization_creates_private_parent_and_preserves_existing_value(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(vm, 'ROOT', Path(temp)):
            path = vm.token_path()
            initial = path.read_text()
            self.assertTrue(initial)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(vm.token_path().read_text(), initial)

    def test_concurrent_token_creators_publish_one_complete_token(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(vm, 'ROOT', Path(temp)):
            barrier = threading.Barrier(2)
            link = os.link
            def publish(source, destination):
                self.assertTrue(Path(source).read_text())
                barrier.wait(timeout=5)
                link(source, destination)
            with patch.object(vm.os, 'link', side_effect=publish), ThreadPoolExecutor(max_workers=2) as workers:
                results = [workers.submit(lambda: vm.token_path().read_text()) for _ in range(2)]
                tokens = [future.result(timeout=10) for future in results]
            self.assertTrue(tokens[0])
            self.assertEqual(tokens[0], tokens[1])
            self.assertEqual(list((Path(temp) / 'operations').glob('.bridge-token-*')), [])

    def test_missing_cua_archive_stops_before_guest_mutation(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(vm, 'execute') as execute:
            with self.assertRaises(FileNotFoundError):
                vm.bootstrap('unused', Path(temp), cua_archive=Path(temp) / 'missing.zip')
            execute.assert_not_called()

    def test_invalid_guest_config_fails_before_upload_or_guest_command(self):
        valid = vm.load_guest_driver_config()
        cases = [None, {'arms': {'cua': valid['arms']['cua']}},
                 {'arms': {'hcu': [], 'cua': valid['arms']['cua']}}]
        for bad_arm in ({'command': []}, {'command': ['relative.exe']},
                        {'command': [r'C:\driver.exe'], 'env': {'NAME': 42}},
                        {'command': [r'C:\driver.exe'], 'env': []},
                        {'command': [r'C:\driver.exe'], 'cwd': 'relative'}):
            cases.append({'arms': {'hcu': valid['arms']['hcu'], 'cua': bad_arm}})
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = root / 'cua.zip'
            archive.touch()
            config_path = root / 'config.json'
            for config in cases:
                with self.subTest(config=config):
                    config_path.write_text(json.dumps(config))
                    with patch.object(vm, 'upload') as upload, patch.object(vm, 'execute') as execute:
                        with self.assertRaises(ValueError):
                            vm.bootstrap('unused', root, cua_archive=archive, guest_driver_config=config_path)
                        upload.assert_not_called()
                        execute.assert_not_called()

    def test_final_cleanup_failure_retains_bounded_stderr_without_inspect_stdout(self):
        binding = {'container': 'owned-vm', 'episode_id': 'mine', 'storage': '/owned/storage'}
        for error in (subprocess.CalledProcessError(1, ['docker', 'inspect'],
                            output='private configuration', stderr='permission denied\x1b[31m' + 'x' * 3000),
                      subprocess.TimeoutExpired(['docker', 'inspect'], 120,
                            output=b'private configuration', stderr=b'daemon timeout\xff')):
            with self.subTest(error=type(error).__name__), patch.object(vm, 'capture_container'), \
                 patch.object(vm, 'command', side_effect=error):
                with self.assertRaises(RuntimeError) as raised:
                    vm.capture_and_stop(binding, Path('/unused'))
                text = str(raised.exception)
                self.assertIn('stderr=', text)
                self.assertNotIn('private configuration', text)
                self.assertNotIn('\x1b', text)
                self.assertLess(len(text), 2400)
                self.assertIs(raised.exception.__cause__, error)
                if isinstance(error, subprocess.CalledProcessError):
                    self.assertIn('permission denied', text)
                    self.assertIn('[truncated]', text)
                else:
                    self.assertIn('daemon timeout', text)

    def test_final_cleanup_failure_keeps_original_and_absence_check_stderr(self):
        binding = {'container': 'owned-vm', 'episode_id': 'mine', 'storage': '/owned/storage'}
        inspected = {'Id': 'a' * 64, 'Config': {'Labels': {'waa.owner': 'waa-v2', 'waa.episode': 'mine'}}}
        error = subprocess.CalledProcessError(1, ['docker', 'stop'], stderr='original stop failure')
        check_error = subprocess.CalledProcessError(1, ['docker', 'inspect'], stderr='absence check denied')
        with patch.object(vm, 'capture_container'), patch.object(vm, 'command',
                    side_effect=[json.dumps(inspected), error, check_error]):
            with self.assertRaises(RuntimeError) as raised:
                vm.capture_and_stop(binding, Path('/unused'))
        self.assertIn('original stop failure', str(raised.exception))
        self.assertIn('absence check denied', str(raised.exception))
        self.assertIs(raised.exception.__cause__, error)

    def test_cleanup_uses_inspected_identity_even_when_container_name_is_reused(self):
        binding = {'container': 'owned-vm', 'episode_id': 'mine', 'storage': '/owned/storage'}
        container_id = 'a' * 64
        inspected = {'Id': container_id, 'Config': {'Labels': {'waa.owner': 'waa-v2', 'waa.episode': 'mine'}}}
        with patch.object(vm, 'command', side_effect=[json.dumps(inspected), '', '']) as command:
            result = vm.stop_binding(binding)
        self.assertEqual(result['status'], 'stopped')
        self.assertEqual(command.call_args_list[1].args[0], ['docker', 'stop', '--time', '120', container_id])
        self.assertEqual(command.call_args_list[2].args[0], ['docker', 'rm', container_id])

    def test_cleanup_accepts_only_explicit_absence_of_the_requested_container(self):
        binding = {'container': 'owned-vm', 'episode_id': 'mine', 'storage': '/owned/storage'}
        for stderr in ('Error: No such object: owned-vm\n',
                       'Error response from daemon: No such container: owned-vm\n',
                       'error: no such object: owned-vm\n',
                       'error response from daemon: no such container: owned-vm\n',
                       'No such container: owned-vm\n',
                       'no such object: owned-vm\n'):
            with self.subTest(stderr=stderr), patch.object(vm, 'command', side_effect=
                    subprocess.CalledProcessError(1, ['docker', 'inspect'], stderr=stderr)) as command:
                self.assertEqual(vm.stop_binding(binding)['status'], 'already_absent')
                self.assertEqual(command.call_count, 1)
        for stderr in ('Cannot connect to the Docker daemon: No such file or directory',
                       'permission denied', 'Error: No such object: another-vm',
                       'error: no such object: OWNED-VM',
                       'error: no such object: owned-vm-other',
                       'Error: No such object: owned-vm\npermission denied',
                       'error: no such object: owned-vm\npermission denied',
                       'error response from daemon: permission denied',
                       'error during connect: no such file or directory',
                       'Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?',
                       'error: no such object: owned-vm (permission denied)'):
            with self.subTest(stderr=stderr), patch.object(vm, 'command', side_effect=
                    subprocess.CalledProcessError(1, ['docker', 'inspect'], stderr=stderr)):
                with self.assertRaises(subprocess.CalledProcessError):
                    vm.stop_binding(binding)

    def test_absence_diagnostic_case_does_not_fold_container_identity(self):
        name = 'Owned.VM'
        for stderr, missing in [('error: no such object: Owned.VM', True),
                                ('ERROR: NO SUCH OBJECT: Owned.VM', True),
                                ('error: no such object: owned.vm', False),
                                ('error: no such object: OwnedXVM', False)]:
            error = subprocess.CalledProcessError(1, ['docker', 'inspect'], stderr=stderr)
            with self.subTest(stderr=stderr), patch.object(vm, 'command', side_effect=error) as command:
                if missing:
                    self.assertIsNone(vm.inspect_container(name))
                else:
                    with self.assertRaises(subprocess.CalledProcessError) as raised:
                        vm.inspect_container(name)
                    self.assertIs(raised.exception, error)
                self.assertEqual(command.call_count, 1)

    def test_absence_wording_does_not_hide_nonstandard_exit_or_timeout(self):
        for error in [subprocess.CalledProcessError(125, ['docker', 'inspect'],
                          stderr='Error: No such object: owned-vm'),
                      subprocess.TimeoutExpired(['docker', 'inspect'], 10,
                          stderr='error: no such object: owned-vm')]:
            with self.subTest(error=error), patch.object(vm, 'command', side_effect=error) as command:
                with self.assertRaises(type(error)) as raised:
                    vm.inspect_container('owned-vm')
                self.assertIs(raised.exception, error)
                self.assertEqual(command.call_count, 1)

    def test_concurrent_cleanup_removal_at_stop_or_rm_is_idempotent(self):
        binding = {'container': 'owned-vm', 'episode_id': 'mine', 'storage': '/owned/storage'}
        container_id = 'a' * 64
        inspected = {'Id': container_id, 'Config': {'Labels': {'waa.owner': 'waa-v2', 'waa.episode': 'mine'}}}
        missing = subprocess.CalledProcessError(1, ['docker', 'inspect'],
                    stderr='error: no such object: ' + container_id)
        for failed_stage in ('stop', 'rm'):
            error = subprocess.CalledProcessError(1, ['docker', failed_stage], stderr='concurrent removal')
            removed = False
            def interleave(argv, **kwargs):
                nonlocal removed
                if argv[1] == 'inspect':
                    if removed:
                        raise missing
                    return json.dumps(inspected['Config']['Labels'] if argv[-1] == '{{json .Config.Labels}}' else inspected)
                if argv[1] == failed_stage:
                    removed = True
                    raise error
                return ''
            with self.subTest(stage=failed_stage), patch.object(vm, 'command', side_effect=interleave) as command:
                self.assertEqual(vm.stop_binding(binding)['status'], 'already_absent')
                self.assertEqual(command.call_args.args[0][:3], ['docker', 'inspect', container_id])

    def test_cleanup_errors_still_fail_when_container_exists_or_recheck_fails(self):
        binding = {'container': 'owned-vm', 'episode_id': 'mine', 'storage': '/owned/storage'}
        inspected = {'Id': 'a' * 64, 'Config': {'Labels': {'waa.owner': 'waa-v2', 'waa.episode': 'mine'}}}
        for failed_stage in ('stop', 'rm'):
            for recheck in (json.dumps(inspected), subprocess.CalledProcessError(
                    1, ['docker', 'inspect'], stderr='permission denied')):
                error = subprocess.CalledProcessError(1, ['docker', failed_stage], stderr='daemon failure')
                responses = [json.dumps(inspected)] + ([''] if failed_stage == 'rm' else []) + [error, recheck]
                with self.subTest(stage=failed_stage, recheck=recheck), \
                     patch.object(vm, 'command', side_effect=responses):
                    with self.assertRaises(subprocess.CalledProcessError) as raised:
                        vm.stop_binding(binding)
                    self.assertIs(raised.exception, error)

    def test_ready_binding_output_failure_cleans_up_instead_of_leaving_vm_running(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot = root / 'snapshot'
            snapshot.mkdir()
            with (snapshot / 'data.img').open('wb') as disk:
                disk.truncate(32212254720)
            archive = root / 'hcu.zip'
            archive.touch()
            token = root / 'token'
            token.write_text('fixture-token')
            args = argparse.Namespace(run_id='broken-output', arm='hcu', snapshot=snapshot,
                                      hcu_archive=archive, cua_archive=archive, hyperv='N', lock_network=False, boot_timeout=5,
                                      image='windows-test:1', network='test-network', bridge_script=archive)
            def print_result(*args, **kwargs):
                if kwargs.get('file') is not sys.stderr:
                    raise BrokenPipeError('reset caller disconnected')
            with patch.object(vm, 'ROOT', root), patch.object(vm, 'command', return_value='192.0.2.1'), \
                 patch.object(vm, 'request', return_value={}), patch.object(vm, 'verify_qemu', return_value={}), \
                 patch.object(vm, 'verify_guest_clock', return_value={}), patch.object(vm, 'verify_guest_license', return_value=[]), \
                 patch.object(vm, 'bootstrap'), patch.object(vm, 'token_path', return_value=token), \
                 patch.object(vm, 'capture_container'), patch.object(vm, 'stop_binding', return_value={'status': 'stopped'}) as stop, \
                 patch('builtins.print', side_effect=print_result):
                with self.assertRaisesRegex(BrokenPipeError, 'caller disconnected'):
                    vm.start(args)
            stop.assert_called_once()
            failure, = (root / 'vms').glob('*/failure.json')
            self.assertEqual(json.loads(failure.read_text())['cleanup']['status'], 'stopped')

    def test_failed_progress_writes_after_create_do_not_skip_owned_vm_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot = root / 'snapshot'
            snapshot.mkdir()
            with (snapshot / 'data.img').open('wb') as disk:
                disk.truncate(32212254720)
            archive = root / 'hcu.zip'
            archive.touch()
            args = argparse.Namespace(run_id='write-failure', arm='hcu', snapshot=snapshot,
                                      hcu_archive=archive, cua_archive=archive, hyperv='N', lock_network=False, boot_timeout=5,
                                      image='windows-test:1', network='test-network', bridge_script=archive)
            original_save = vm.save_binding
            calls = 0
            def save(directory, binding):
                nonlocal calls
                calls += 1
                if calls > 1:
                    raise OSError('progress disk full')
                original_save(directory, binding)
            with patch.object(vm, 'ROOT', root), patch.object(vm, 'save_binding', side_effect=save), \
                 patch.object(vm, 'command', return_value='192.0.2.1'), \
                 patch.object(vm, 'capture_container', side_effect=OSError('capture disk full')), \
                 patch.object(vm, 'stop_binding', return_value={'status': 'stopped'}) as stop:
                with self.assertRaisesRegex(OSError, 'progress disk full'):
                    vm.start(args)
            stop.assert_called_once()
            binding = stop.call_args.args[0]
            self.assertEqual(binding['status'], 'failed')
            self.assertEqual(binding['cleanup']['status'], 'stopped')

    def test_stop_still_removes_owned_container_when_capture_storage_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            owner = root / 'episode.json'
            owner.write_text(json.dumps({'container': 'owned-vm', 'episode_id': 'mine', 'storage': str(root / 'storage')}))
            with patch('sys.argv', ['vm.py', 'stop', '--episode', str(owner)]), \
                 patch.object(vm, 'capture_container', side_effect=OSError('capture denied')), \
                 patch.object(vm, 'stop_binding', return_value={'status': 'stopped'}) as stop:
                vm.main()
            stop.assert_called_once()
            saved = json.loads(owner.read_text())
            self.assertEqual(saved['cleanup']['status'], 'stopped')
            self.assertIn('capture denied', saved['cleanup']['capture_error'])

    def test_removed_container_recapture_preserves_original_evidence_and_failed_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def successful(argv, **kwargs):
                output = 'original guest boot log' if argv[1] == 'logs' else '{"State":"running"}'
                return subprocess.CompletedProcess(argv, 0, output, '')
            with patch.object(vm.subprocess, 'run', side_effect=successful):
                vm.capture_container('owned-vm', root)
            original = {name: (root / name).read_text() for name in ('container.log', 'container-inspect.json')}
            with patch.object(vm.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'No such container: owned-vm')):
                vm.capture_container('owned-vm', root)
                vm.capture_container('owned-vm', root)
            for name, text in original.items():
                self.assertEqual((root / name).read_text(), text)
            attempts = list((root / 'container-captures').iterdir())
            self.assertEqual(len(attempts), 3)
            records = [json.loads((attempt / 'capture.json').read_text()) for attempt in attempts]
            self.assertEqual(sum(record['files']['container.log'].get('returncode') == 1 for record in records), 2)
            self.assertEqual(sum('No such container' in (attempt / 'container.log').read_text() for attempt in attempts), 2)

    def test_recapture_timeout_keeps_usable_files_and_records_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'container.log').write_text('prior log')
            (root / 'container-inspect.json').write_text('prior inspect')
            with patch.object(vm.subprocess, 'run', side_effect=subprocess.TimeoutExpired('docker', 20)):
                vm.capture_container('owned-vm', root)
            self.assertEqual((root / 'container.log').read_text(), 'prior log')
            self.assertEqual((root / 'container-inspect.json').read_text(), 'prior inspect')
            record_path, = (root / 'container-captures').glob('*/capture.json')
            record = json.loads(record_path.read_text())
            self.assertIn('TimeoutExpired', record['files']['container.log']['error'])

    def test_later_success_replaces_failed_summary_without_losing_attempt_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(vm.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'initial Docker error')):
                vm.capture_container('owned-vm', root)
            self.assertEqual((root / 'container.log').read_text(), 'initial Docker error')
            with patch.object(vm.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'recovered evidence', '')):
                vm.capture_container('owned-vm', root)
            self.assertEqual((root / 'container.log').read_text(), 'recovered evidence')
            attempts = list((root / 'container-captures').iterdir())
            self.assertEqual(len(attempts), 2)
            self.assertEqual({(attempt / 'container.log').read_text() for attempt in attempts},
                             {'initial Docker error', 'recovered evidence'})

    def test_sparse_snapshot_copy_is_independently_writable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source'
            source.mkdir()
            with (source / 'data.img').open('wb') as stream:
                stream.write(b'baseline')
                stream.seek(8 * 1024 * 1024)
                stream.write(b'end')
            target = root / 'copy'
            target.mkdir()
            vm.command(['cp', '-a', '--reflink=auto', '--sparse=always', str(source) + '/.', str(target)])
            with (target / 'data.img').open('r+b') as stream:
                stream.write(b'mutation')
            self.assertEqual((source / 'data.img').read_bytes()[:8], b'baseline')
            self.assertNotEqual((source / 'data.img').stat().st_ino, (target / 'data.img').stat().st_ino)

    def test_root_can_be_selected_without_changing_default(self):
        result = subprocess.run([os.sys.executable, '-c', 'import vm; print(vm.ROOT)'],
                                cwd=Path(vm.__file__).parent, env={**os.environ, 'WAA_ROOT': '/tmp/waa-test-root'},
                                check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), '/tmp/waa-test-root')

    def test_flat_and_nested_packages_replace_flattened_install_twice(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            hcu, cua = root / 'hcu.zip', root / 'cua.zip'
            with zipfile.ZipFile(hcu, 'w') as package:
                package.writestr('computer-use.exe', 'fixture executable')
                package.writestr('computer-use-windows-provider.exe', 'fixture provider')
            with zipfile.ZipFile(cua, 'w') as package:
                package.writestr('release/cua-driver.exe', 'fixture executable')
                package.writestr('release/cua-driver-uia.exe', 'fixture provider')
                package.writestr('release/cua_driver_sdk.dll', 'fixture SDK')
            untouched = root / 'bridge-token.txt'
            untouched.write_text('untouched-test-token')
            for folder, executable in [('hcu', 'computer-use.exe'), ('cua-driver', 'cua-driver.exe')]:
                (root / folder).mkdir()
                (root / folder / executable).write_text('old flattened executable')
                (root / folder / 'old-provider.dll').write_text('obsolete provider')
            for _ in range(2):
                exec(compile(vm.DRIVER_INSTALL, 'guest-driver-install', 'exec'), {'p': root})
                for archive, folder, executable, nested in [(hcu, 'hcu', 'computer-use.exe', False),
                                                              (cua, 'cua-driver', 'cua-driver.exe', True)]:
                    dest = root / folder
                    self.assertEqual(list(dest.rglob(executable)), [dest / executable])
                    self.assertFalse((dest / 'old-provider.dll').exists())
                    with zipfile.ZipFile(archive) as package:
                        files = [item for item in package.infolist() if not item.is_dir()]
                        self.assertEqual(len([p for p in dest.rglob('*') if p.is_file()]), len(files))
                        for item in files:
                            relative = Path(*Path(item.filename).parts[1:]) if nested else Path(item.filename)
                            self.assertEqual((dest / relative).stat().st_size, item.file_size)
                self.assertTrue((root / 'hcu/computer-use-windows-provider.exe').is_file())
                self.assertTrue((root / 'cua-driver/cua-driver-uia.exe').is_file())
                self.assertTrue((root / 'cua-driver/cua_driver_sdk.dll').is_file())
                self.assertEqual(untouched.read_text(), 'untouched-test-token')

    def test_episode_progress_mirrors_cleanup_before_final_stdout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            owner = root / 'vm'
            owner.mkdir()
            mirror = root / 'reset-episode.json'
            binding = {'status': 'creating', 'episode_id': 'one', 'cleanup_command': ['vm.py', 'stop']}
            with patch.dict(os.environ, {'WAA_RESET_RECORD_FILE': str(mirror)}):
                vm.save_binding(owner, binding)
            self.assertEqual(json.loads(mirror.read_text()), binding)
            self.assertEqual(json.loads((owner / 'episode.json').read_text()), binding)
            self.assertFalse(mirror.with_suffix('.tmp').exists())

    def test_explicit_image_and_timezone_keep_kvm_with_hyperv_disabled(self):
        with patch('sys.argv', ['vm.py', 'reset', '--run-id', 'test', '--image', 'windows-test:1',
                               '--network', 'test-network', '--snapshot', '/snapshot',
                               '--hcu-archive', '/hcu.zip', '--cua-archive', '/cua.zip',
                               '--timezone', 'Europe/London']), patch.object(vm, 'start') as start:
            vm.main()
        args = start.call_args.args[0]
        self.assertEqual(args.hyperv, 'N')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            command = vm.container_command(args, 'test-container', root / 'storage', root)
        self.assertIn('--device=/dev/kvm', command)
        self.assertIn('KVM=Y', command)
        self.assertIn('TZ=Europe/London', command)
        self.assertIn('windows-test:1', command)
        self.assertIn('test-network', command)

    def test_entrypoint_installs_lock_after_network_before_vm(self):
        # Exercise the actual awk injector against a tiny entrypoint whose network resets state.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            entry = root / 'entry.sh'
            entry.write_text('before\n. network.sh # resets firewall\nstart-qemu\n')
            injector = vm.LOCK_ENTRYPOINT.split('\nchmod', 1)[0].replace('/run/entry.sh', str(entry)).replace('/run/waa-entry.sh', str(root / 'generated.sh'))
            subprocess.run(['bash', '-c', injector], check=True, capture_output=True)
            output = (root / 'generated.sh').read_text().splitlines()
            self.assertLess(output.index('. network.sh # resets firewall'), output.index('. /waa-network-lock.sh'))
            self.assertLess(output.index('. /waa-network-lock.sh'), output.index('start-qemu'))
            entry.write_text('start-qemu\n')
            with self.assertRaises(subprocess.CalledProcessError):
                subprocess.run(['bash', '-c', injector], check=True, capture_output=True)

    def test_foreign_episode_cannot_be_stopped(self):
        binding = {'container': 'baseline', 'episode_id': 'mine', 'storage': '/disk'}
        for labels in ({'waa.owner': 'waa-v2', 'waa.episode': 'other'},
                       {'waa.owner': 'foreign', 'waa.episode': 'mine'}, None):
            inspected = {'Id': 'a' * 64, 'Config': {'Labels': labels}}
            with self.subTest(labels=labels), patch.object(vm, 'command', return_value=json.dumps(inspected)) as command:
                with self.assertRaisesRegex(RuntimeError, 'ownership'):
                    vm.stop_binding(binding)
                self.assertEqual(command.call_count, 1)

    def test_cleanup_rejects_invalid_inspected_identity_before_mutation(self):
        binding = {'container': 'owned-vm', 'episode_id': 'mine', 'storage': '/disk'}
        for container_id in ('', 'owned-vm', None):
            inspected = {'Id': container_id, 'Config': {'Labels': {'waa.owner': 'waa-v2', 'waa.episode': 'mine'}}}
            with self.subTest(container_id=container_id), patch.object(vm, 'command', return_value=json.dumps(inspected)) as command:
                with self.assertRaisesRegex(RuntimeError, 'container ID'):
                    vm.stop_binding(binding)
                self.assertEqual(command.call_count, 1)

    def test_bootstrap_failure_seals_evidence_and_stops_owned_vm(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot = root / 'snapshot'
            snapshot.mkdir()
            with (snapshot / 'data.img').open('wb') as disk:
                disk.truncate(32212254720)
            archive = root / 'hcu.zip'
            archive.touch()
            args = argparse.Namespace(run_id='test', arm='cua', snapshot=snapshot, hcu_archive=archive,
                                      cua_archive=archive, hyperv='N', lock_network=False, boot_timeout=5,
                                      image='windows-test:1', network='test-network', bridge_script=archive)
            order = []
            def command(argv, **kwargs):
                return '192.0.2.1' if argv[:2] == ['docker', 'inspect'] else ''
            def capture(name, directory):
                self.assertEqual(json.loads((directory / 'episode.json').read_text())['status'], 'failed')
                order.append('capture')
            def stop(binding):
                order.append('stop')
                return {'status': 'stopped'}
            with patch.object(vm, 'ROOT', root), patch.object(vm, 'command', side_effect=command), \
                 patch.object(vm, 'request', return_value={}), patch.object(vm, 'verify_guest_clock', return_value={}), \
                 patch.object(vm, 'verify_guest_license', return_value=[]), \
                 patch.object(vm, 'verify_qemu', return_value={}), \
                 patch.object(vm, 'bootstrap', side_effect=RuntimeError('bootstrap failed')) as bootstrap, \
                 patch.object(vm, 'capture_container', side_effect=capture), patch.object(vm, 'stop_binding', side_effect=stop):
                with self.assertRaisesRegex(RuntimeError, 'bootstrap failed'):
                    vm.start(args)
            evidence = list((root / 'vms').glob('*/failure.json'))
            self.assertEqual(len(evidence), 1)
            saved = json.loads(evidence[0].read_text())
            self.assertEqual(saved['cleanup']['status'], 'stopped')
            self.assertEqual(saved['snapshot'], str(snapshot.resolve()))
            self.assertEqual(saved['hcu_archive'], str(archive.resolve()))
            self.assertEqual(saved['cua_archive'], str(archive.resolve()))
            self.assertEqual(bootstrap.call_args.args[3], archive.resolve())
            self.assertEqual(order, ['capture', 'stop'])

    def test_guest_timezone_mismatch_fails_without_mutating_guest(self):
        with patch.object(vm, 'execute', return_value={'output': json.dumps({'timezone': 'UTC', 'utc': '2026-10-21T00:00:00+00:00'})}) as execute:
            with self.assertRaisesRegex(RuntimeError, 'timezone'):
                vm.verify_guest_clock('http://example.test:5000')
        self.assertEqual(execute.call_count, 1)
        self.assertNotIn('tzutil /s', execute.call_args.args[1])

    def test_unlicensed_guest_is_rejected_without_activation_or_clock_write(self):
        with patch.object(vm, 'execute', return_value={'output': '{"Name":"Windows","LicenseStatus":0}'}) as execute:
            with self.assertRaisesRegex(RuntimeError, 'no active license'):
                vm.verify_guest_license('http://example.test:5000')
        code = execute.call_args.args[1]
        self.assertNotIn('Set-Date', code)
        self.assertNotIn('slmgr', code)


if __name__ == '__main__':
    unittest.main()
