"""Playback capture is opt-in, isolated per episode, and checked against live argv."""
import argparse
import json
import os
from pathlib import Path
import shlex
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

import remote_vm
import qmp_audio
import vm


class AudioCaptureTests(unittest.TestCase):
    def options(self, capture=False):
        return argparse.Namespace(image='prepared:1', network='test-network', hyperv='N',
                                  lock_network=False, capture_audio=capture)

    def test_default_does_not_add_audio_device_backend_or_mount(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            argv = vm.container_command(self.options(), 'owned', root / 'storage', root)
            arguments = next(value.removeprefix('ARGUMENTS=') for value in argv if value.startswith('ARGUMENTS='))
            self.assertEqual(shlex.split(arguments), ['-qmp', 'tcp:0.0.0.0:7200,server,nowait'])
            self.assertFalse((root / 'audio').exists())
            self.assertFalse(any('/waa-audio' in value for value in argv))

    def test_enabled_capture_preserves_qmp_and_uses_separate_episode_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            for name in ('first', 'second'):
                root = Path(temp) / name
                root.mkdir()
                argv = vm.container_command(self.options(True), 'owned-' + name, root / 'storage', root)
                arguments = next(value.removeprefix('ARGUMENTS=') for value in argv if value.startswith('ARGUMENTS='))
                qemu = shlex.split(arguments)
                self.assertEqual(qemu[:2], ['-qmp', 'tcp:0.0.0.0:7200,server,nowait'])
                self.assertIn(str(root / 'audio') + ':/waa-audio', argv)
                self.assertNotIn(str(root / 'storage/audio') + ':/waa-audio', argv)
                self.assertEqual((root / 'audio').stat().st_mode & 0o777, 0o700)
                capture = root / 'audio/playback.wav'
                self.assertEqual(capture.stat().st_mode & 0o777, 0o600)
                self.assertEqual(capture.stat().st_uid, os.getuid())
                self.assertEqual(capture.read_bytes(), b'')
                self.assertIn('hda-output,bus=waa-hda.0,audiodev=waa-audio', qemu)
                self.assertNotIn('hda-duplex', arguments)

    def test_live_qemu_must_include_both_device_and_backend_when_requested(self):
        observed = {'argv': ['qemu-system-x86_64', '-enable-kvm', '-rtc', 'base=localtime'], 'timezone': 'UTC'}
        with patch.object(vm, 'command', return_value=json.dumps([observed])):
            self.assertNotIn('playback_capture_arguments_verified', vm.verify_qemu('owned'))
            with self.assertRaisesRegex(RuntimeError, 'playback capture'):
                vm.verify_qemu('owned', capture_audio=True)
        observed['argv'] += list(vm.AUDIO_CAPTURE_ARGS)
        with patch.object(vm, 'command', return_value=json.dumps([observed])):
            self.assertTrue(vm.verify_qemu('owned', capture_audio=True)['playback_capture_arguments_verified'])
        observed['argv'][-1] = 'hda-duplex,bus=waa-hda.0,audiodev=waa-audio'
        with patch.object(vm, 'command', return_value=json.dumps([observed])):
            with self.assertRaisesRegex(RuntimeError, 'playback capture'):
                vm.verify_qemu('owned', capture_audio=True)

    def test_cli_flag_is_forwarded_without_enabling_it_by_default(self):
        base = ['remote_vm.py', 'reset', '--ssh-host', 'vm-host.example', '--remote-root', '/srv/waa',
                '--remote-repo', '/srv/repo', '--run-id', 'audio-test', '--image', 'prepared:1',
                '--network', 'test-network', '--snapshot', '/srv/snapshot', '--hcu-archive', '/srv/hcu.zip',
                '--cua-archive', '/srv/cua.zip']
        for enabled in (False, True):
            with patch('sys.argv', base + (['--capture-audio'] if enabled else [])), \
                 patch.object(remote_vm, 'reset') as reset, patch.object(remote_vm.signal, 'signal'):
                remote_vm.main()
                self.assertEqual(reset.call_args.args[0].capture_audio, enabled)

    def test_monitor_negotiates_qmp_and_ignores_events(self):
        client, server = socket.socketpair()
        received = []
        def peer():
            with server, server.makefile('rwb') as stream:
                stream.write(b'{"QMP": {}}\n')
                stream.flush()
                for result in ({}, 'capture status'):
                    request = json.loads(stream.readline())
                    received.append(request)
                    stream.write(b'{"event": "RESUME"}\n')
                    stream.write((json.dumps({'return': result, 'id': request['id']}) + '\n').encode())
                    stream.flush()
        thread = threading.Thread(target=peer)
        thread.start()
        try:
            with patch.object(qmp_audio.socket, 'create_connection', return_value=client):
                self.assertEqual(qmp_audio.monitor(['info capture']), ['capture status'])
        finally:
            thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(received[0]['execute'], 'qmp_capabilities')
        self.assertEqual(received[1]['arguments']['command-line'], 'info capture')

    def test_monitor_is_pinned_to_container_object_not_reusable_ip_or_name(self):
        binding = {'container_id': 'a' * 64, 'vm_ip': '192.0.2.1', 'container': 'reusable-name'}
        with patch.object(vm, 'command', return_value='[""]') as command:
            self.assertEqual(vm.audio_monitor(binding, ['info capture'], 'b' * 64), [''])
        argv = command.call_args.args[0]
        self.assertEqual(argv[:4], ['docker', 'exec', 'b' * 64, 'python3'])
        self.assertNotIn(binding['vm_ip'], argv)
        self.assertNotIn(binding['container'], argv)

    def test_capture_lifecycle_checks_registration_and_finalization(self):
        binding = {'audio_capture': {}}
        info = '[3]: Capturing audio(44100,16,2) to /waa-audio/playback.wav: 123456 bytes\r\n'
        with patch.object(vm, 'audio_monitor', side_effect=[[''], ['', info], [info], ['', '']]) as monitor:
            vm.start_audio_capture(binding)
            self.assertTrue(binding['audio_capture']['started'])
            vm.stop_audio_capture(binding)
            self.assertFalse(binding['audio_capture']['started'])
            self.assertTrue(binding['audio_capture']['monitor_stopped'])
            self.assertEqual(monitor.call_args.args[1], ['stopcapture 3', 'info capture'])

    def test_capture_rejects_other_paths_or_multiple_registrations(self):
        info = '[0]: Capturing audio(44100,16,2) to /waa-audio/playback.wav: 0 bytes\n'
        for invalid in ('', info.replace('playback.wav', 'other.wav'), info + info):
            with self.assertRaisesRegex(RuntimeError, 'owned playback capture'):
                vm.capture_index(invalid)
        with patch.object(vm, 'audio_monitor', return_value=[info]) as monitor:
            with self.assertRaisesRegex(RuntimeError, 'existing'):
                vm.start_audio_capture({'audio_capture': {}})
            self.assertEqual(monitor.call_count, 1)

    def test_audio_failure_does_not_prevent_owned_container_cleanup(self):
        binding = {'container': 'owned', 'episode_id': 'episode', 'storage': '/tmp/storage',
                   'audio_capture': {'started': True}}
        container = {'Id': 'a' * 64, 'Config': {'Labels': {'waa.owner': 'waa-v2', 'waa.episode': 'episode'}}}
        with patch.object(vm, 'inspect_container', return_value=container), \
             patch.object(vm, 'stop_audio_capture', side_effect=RuntimeError('QMP failed')), \
             patch.object(vm, 'command', return_value='') as command:
            result = vm.stop_binding(binding)
        self.assertEqual(result['status'], 'stopped')
        self.assertIn('QMP failed', result['audio_capture_error'])
        self.assertEqual([call.args[0][1] for call in command.call_args_list], ['stop', 'rm'])


if __name__ == '__main__':
    unittest.main()
