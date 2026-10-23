import argparse
import errno
import json
import multiprocessing
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import remote_vm
import vm


def _cancellable_worker_probe(record, connection):
    def start_without_vm(_args):
        connection.send('started')
        try:
            signal.pause()
        finally:
            connection.send('cleanup_finished')
    args = argparse.Namespace(worker_record=Path(record),
                              reset_options=json.dumps({'snapshot': '/unused', 'hcu_archive': '/unused'}))
    with patch.object(vm, 'start', side_effect=start_without_vm):
        try:
            remote_vm.worker(args)
        except SystemExit:
            pass


class RemoteVMTests(unittest.TestCase):
    def test_worker_decodes_explicit_cua_archive_without_local_path_resolution(self):
        for selected in (None, '/remote/candidate with spaces.zip'):
            with self.subTest(selected=selected), tempfile.TemporaryDirectory() as temp:
                record = Path(temp) / 'worker.json'
                args = argparse.Namespace(worker_record=record, reset_options=json.dumps({
                    'snapshot': '/remote/snapshot', 'hcu_archive': '/remote/hcu.zip',
                    'cua_archive': selected, 'guest_driver_config': selected}))
                with patch.object(remote_vm.signal, 'signal'), patch.dict(os.environ), \
                     patch.object(vm, 'start') as start:
                    remote_vm.worker(args)
                self.assertEqual(start.call_args.args[0].cua_archive,
                                 Path(selected) if selected is not None else None)
                self.assertEqual(start.call_args.args[0].guest_driver_config,
                                 Path(selected) if selected is not None else None)

    def test_real_worker_cancel_waits_for_cleanup_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as temp:
            record = Path(temp) / 'worker.json'
            context = multiprocessing.get_context('fork')
            receive, send = context.Pipe(duplex=False)
            process = context.Process(target=_cancellable_worker_probe, args=(str(record), send))
            process.start()
            send.close()
            try:
                self.assertTrue(receive.poll(5), 'worker did not reach its reset body')
                self.assertEqual(receive.recv(), 'started')
                remote_vm.cancel_worker(argparse.Namespace(worker_record=record))
                self.assertTrue(receive.poll(3), 'worker did not finish cleanup')
                self.assertEqual(receive.recv(), 'cleanup_finished')
                process.join(timeout=3)
                self.assertFalse(process.is_alive())
                self.assertEqual(json.loads(record.read_text())['status'], 'finished')
                remote_vm.cancel_worker(argparse.Namespace(worker_record=record))
            finally:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=3)
                receive.close()
    def binding(self, root):
        return {'ssh_host': 'vm-host.example', 'remote_root': '/remote/data', 'remote_python': '/usr/bin/python3',
                'remote_script': '/remote/repo/integration/remote_vm.py',
                'worker_record': '/remote/data/remote-workers/unique.json',
                'control_path': str(root / 'owned-control'), 'vm_ip': '127.0.0.42',
                'remote_binding': {'vm_ip': '192.0.2.10'}}

    def test_ssh_arguments_preserve_shell_metacharacters_as_literal_arguments(self):
        argv = ['python3', '/path with space/script.py', '$(must_not_run)', 'a\nb']
        command = remote_vm.ssh_command('vm-host.example', argv)
        self.assertEqual(shlex.split(command[-1]), argv)
        with self.assertRaises(ValueError):
            remote_vm.ssh_command('-bad-option', argv)

    def test_four_loopback_endpoints_allow_independent_listeners(self):
        listeners = []
        try:
            for last in range(42, 46):
                address = f'127.31.0.{last}'
                listener = socket.socket()
                listeners.append(listener)
                listener.bind((address, 0 if last == 42 else port))
                port = listener.getsockname()[1]
                listener.listen(1)
            with patch.object(remote_vm, 'PORTS', (port,)):
                for last in range(42, 46):
                    with self.assertRaises(OSError):
                        remote_vm.check_ports(f'127.31.0.{last}')
                listeners[0].close()
                remote_vm.check_ports('127.31.0.42')
                with self.assertRaises(OSError):
                    remote_vm.check_ports('127.31.0.43')
        finally:
            for listener in listeners:
                listener.close()

    def test_cancel_stops_vm_even_when_capture_cannot_be_saved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            record = root / 'worker.json'
            progress = record.with_suffix('.vm.json')
            progress.write_text(json.dumps({'container': 'owned-vm', 'episode_id': 'mine', 'storage': str(root / 'storage')}))
            with patch.object(vm, 'capture_container', side_effect=OSError('capture denied')), \
                 patch.object(vm, 'stop_binding', return_value={'status': 'stopped'}) as stop:
                remote_vm.cancel_worker(argparse.Namespace(worker_record=record))
            stop.assert_called_once()
            saved = json.loads(progress.read_text())
            self.assertIn('capture denied', saved['cleanup']['capture_error'])

    def test_reset_progress_write_failure_still_invokes_remote_cleanup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = argparse.Namespace(loopback_ip='127.0.0.42', ssh_host='vm-host.example', remote_root='/remote/data',
                remote_repo='/remote/repo', remote_python='/usr/bin/python3', run_id='test', arm='hcu',
                boot_timeout=600, snapshot='/remote/snapshot', hcu_archive='/remote/hcu.zip',
                lock_network=False, reset_timeout=1)
            original_save = remote_vm.save_local
            calls = 0
            def save(path, binding):
                nonlocal calls
                calls += 1
                if calls > 1:
                    raise OSError('local progress disk full')
                original_save(path, binding)
            class Process:
                def wait(self, timeout=None):
                    raise subprocess.TimeoutExpired('ssh', timeout)
                def poll(self):
                    return 0
            with patch.object(remote_vm, 'ROOT', root), patch.object(remote_vm, 'check_ports'), \
                 patch.object(remote_vm, 'save_local', side_effect=save), \
                 patch.object(remote_vm.subprocess, 'Popen', return_value=Process()), \
                 patch.object(remote_vm, 'stop') as stop:
                with self.assertRaises(subprocess.TimeoutExpired):
                    remote_vm.reset(args)
            stop.assert_called_once()
            binding = stop.call_args.args[0]
            Path(binding['control_path']).parent.rmdir()

    def test_all_upstream_ports_bind_only_loopback_and_unique_control(self):
        with tempfile.TemporaryDirectory() as temp:
            binding = self.binding(Path(temp))
            command = remote_vm.tunnel_command(binding)
        forwards = [command[i + 1] for i, item in enumerate(command) if item == '-L']
        self.assertEqual(forwards, [f'127.0.0.42:{port}:192.0.2.10:{port}' for port in remote_vm.PORTS])
        self.assertIn('ExitOnForwardFailure=yes', command)
        self.assertEqual(command[command.index('-S') + 1], binding['control_path'])

    def test_port_conflict_detected_before_remote_creation(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
            with patch.object(remote_vm, 'PORTS', (port,)):
                with self.assertRaises(OSError):
                    remote_vm.check_ports('127.0.0.1')
        with self.assertRaises(ValueError):
            remote_vm.check_ports('0.0.0.0')

    def test_completed_connection_time_wait_does_not_block_next_episode(self):
        with socket.socket() as listener, socket.socket() as client:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
            listener.listen(1)
            client.settimeout(3)
            client.connect(('127.0.0.1', port))
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(3)
                # The forwarding side closes first, so its local port enters TIME_WAIT.
                connection.shutdown(socket.SHUT_WR)
                self.assertEqual(client.recv(1), b'')
                client.shutdown(socket.SHUT_WR)
                self.assertEqual(connection.recv(1), b'')
        with socket.socket() as without_reuse:
            with self.assertRaises(OSError) as conflict:
                without_reuse.bind(('127.0.0.1', port))
            self.assertEqual(conflict.exception.errno, errno.EADDRINUSE)
        with patch.object(remote_vm, 'PORTS', (port,)):
            remote_vm.check_ports('127.0.0.1')

    def test_existing_reusable_listener_still_blocks_preflight(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            with patch.object(remote_vm, 'PORTS', (port,)):
                with self.assertRaises(OSError) as conflict:
                    remote_vm.check_ports('127.0.0.1')
            self.assertEqual(conflict.exception.errno, errno.EADDRINUSE)

    def test_pidfd_signal_rejects_reused_or_unowned_identity(self):
        process = subprocess.Popen(['sleep', '30'])
        try:
            actual = remote_vm.start_ticks(process.pid)
            remote_vm.signal_worker({'pid': process.pid, 'start_ticks': 'wrong-generation'})
            self.assertIsNone(process.poll())
            remote_vm.signal_worker({'pid': process.pid, 'start_ticks': actual})
            self.assertEqual(process.wait(timeout=3), -15)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)

    def test_cancel_before_worker_prevents_container_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            record = Path(temp) / 'worker.json'
            args = argparse.Namespace(worker_record=record, reset_options='{}')
            with patch.object(vm, 'start') as start, patch.object(remote_vm.signal, 'signal'):
                remote_vm.cancel_worker(args)
                with self.assertRaisesRegex(SystemExit, 'cancelled before reset'):
                    remote_vm.worker(args)
            start.assert_not_called()
            self.assertEqual(json.loads(record.read_text())['status'], 'finished')

    def test_failed_remote_cleanup_still_closes_only_owned_tunnel(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binding = self.binding(root)
            Path(binding['control_path']).touch()
            calls = []
            def run(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 1 if len(calls) == 1 else 0, '', '')
            with patch.object(remote_vm.subprocess, 'run', side_effect=run):
                with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
                    remote_vm.stop(binding, root / 'episode.json')
            self.assertEqual(calls[1], ['ssh', '-S', binding['control_path'], '-O', 'exit', 'vm-host.example'])
            self.assertEqual(json.loads((root / 'episode.json').read_text())['cleanup']['remote']['returncode'], 1)

    def test_reset_timeout_has_early_cleanup_binding_and_cancels_remote(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = argparse.Namespace(loopback_ip='127.0.0.42', ssh_host='vm-host.example', remote_root='/remote/data',
                remote_repo='/remote/repo', remote_python='/usr/bin/python3', run_id='test', arm='hcu',
                boot_timeout=600, snapshot='/remote/snapshot', hcu_archive='/remote/hcu.zip',
                cua_archive='/remote/candidate with spaces.zip',
                guest_driver_config='/remote/driver config.json',
                image='windows-image:1', network='isolated-network', timezone='Europe/London',
                windows_timezone='GMT Standard Time', bridge_script='/remote/bridge.py',
                activation_worker='/remote/worker.py', activation_revision='worker-version-2',
                activation_server_dir=r'C:\Custom Server',
                lock_network=False, reset_timeout=1)
            mirror = root / 'early.json'
            class Process:
                def wait(self, timeout=None):
                    self.binding = json.loads(mirror.read_text())
                    raise subprocess.TimeoutExpired('ssh', timeout)
                def poll(self):
                    return 0
            process = Process()
            calls = []
            def run(command, **kwargs):
                calls.append(command)
                return subprocess.CompletedProcess(command, 0, '{}', '')
            with patch.object(remote_vm, 'ROOT', root), patch.object(remote_vm, 'check_ports'), \
                 patch.object(remote_vm.subprocess, 'Popen', return_value=process) as popen, \
                 patch.object(remote_vm.subprocess, 'run', side_effect=run), \
                 patch.dict(os.environ, {'WAA_RESET_RECORD_FILE': str(mirror)}):
                with self.assertRaises(subprocess.TimeoutExpired):
                    remote_vm.reset(args)
            self.assertEqual(process.binding['status'], 'creating')
            worker_argv = shlex.split(popen.call_args.args[0][-1])
            worker_options = json.loads(worker_argv[worker_argv.index('--reset-options') + 1])
            self.assertEqual(worker_options['cua_archive'], args.cua_archive)
            self.assertEqual(worker_options['guest_driver_config'], args.guest_driver_config)
            for field in ('image', 'network', 'timezone', 'windows_timezone', 'bridge_script',
                          'activation_worker', 'activation_revision', 'activation_server_dir'):
                self.assertEqual(worker_options[field], getattr(args, field))
            self.assertIn('cleanup_command', process.binding)
            self.assertIn('cancel', shlex.split(calls[0][-1]))
            saved = json.loads(mirror.read_text())
            self.assertEqual(saved['cleanup']['remote']['returncode'], 0)
            Path(saved['control_path']).parent.rmdir()


if __name__ == '__main__':
    unittest.main()
