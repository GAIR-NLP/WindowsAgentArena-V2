"""Real child processes exercise reset TERM and KILL boundaries without a VM."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from waa_compare import run_reset_hook


class ResetProcessTests(unittest.TestCase):
    def run_ready_child(self, folder, signal_code, grace):
        reader, writer = os.pipe()
        actual_popen = subprocess.Popen
        script = f"import os,signal,pathlib,sys\n{signal_code}\nos.write({writer},b'R')\nsignal.pause()\n"
        def ready_popen(*args, **kwargs):
            child = actual_popen(*args, **kwargs, pass_fds=(writer,))
            self.assertTrue(select.select([reader], [], [], 3)[0], 'Child did not reach signal-ready state')
            self.assertEqual(os.read(reader, 1), b'R')
            return child
        try:
            with patch('waa_compare.subprocess.Popen', side_effect=ready_popen):
                with self.assertRaises(TimeoutError):
                    run_reset_hook([sys.executable, '-c', script], folder, 0.01, grace)
        finally:
            os.close(reader)
            os.close(writer)

    def test_term_allows_cleanup_and_seals_process_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            code = f"def cleanup(*_):\n pathlib.Path({str(folder / 'cleaned')!r}).write_text('done')\n sys.exit(0)\nsignal.signal(signal.SIGTERM,cleanup)"
            self.run_ready_child(folder, code, 2)
            saved = json.loads((folder / 'reset-process.json').read_text())
            self.assertEqual((folder / 'cleaned').read_text(), 'done')
            self.assertEqual(saved['returncode'], 0)
            self.assertTrue(saved['sigterm_sent'])
            self.assertNotIn('sigkill_sent', saved)

    def test_stalled_cleanup_receives_kill_after_grace(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            self.run_ready_child(folder, 'signal.signal(signal.SIGTERM,signal.SIG_IGN)', 0.02)
            saved = json.loads((folder / 'reset-process.json').read_text())
            self.assertEqual(saved['status'], 'forced_kill')
            self.assertEqual(saved['returncode'], -9)
            self.assertTrue(saved['sigterm_sent'])
            self.assertTrue(saved['sigkill_sent'])

    def test_success_preserves_stdout_binding_and_progress_location(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            result = run_reset_hook([sys.executable, '-c', "import os,json; print(json.dumps({'record':os.environ['WAA_RESET_RECORD_FILE']}))"], folder, 3, 1)
            self.assertEqual(json.loads(result)['record'], str(folder / 'reset-episode.json'))
            self.assertEqual(json.loads((folder / 'reset-process.json').read_text())['status'], 'completed')

    def test_first_progress_write_failure_terminates_ready_child(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            reader, writer = os.pipe()
            actual_popen = subprocess.Popen
            processes = []
            marker = folder / 'cleaned'
            script = ("import os,signal,pathlib,sys\n"
                      f"def cleanup(*_):\n pathlib.Path({str(marker)!r}).touch()\n sys.exit(0)\n"
                      "signal.signal(signal.SIGTERM,cleanup)\n"
                      f"os.write({writer},b'R')\nsignal.pause()\n")
            def ready_popen(*args, **kwargs):
                child = actual_popen(*args, **kwargs, pass_fds=(writer,))
                processes.append(child)
                self.assertTrue(select.select([reader], [], [], 3)[0])
                self.assertEqual(os.read(reader, 1), b'R')
                return child
            try:
                with patch('waa_compare.subprocess.Popen', side_effect=ready_popen), \
                        patch('waa_compare.write_json', side_effect=OSError('full evidence disk')):
                    with self.assertRaisesRegex(OSError, 'full evidence disk') as raised:
                        run_reset_hook([sys.executable, '-c', script], folder, 10, 3)
                self.assertEqual(processes[0].poll(), 0)
                self.assertTrue(marker.exists())
                self.assertTrue(any('Reset progress write failed' in note for note in raised.exception.__notes__))
            finally:
                os.close(reader)
                os.close(writer)
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=3)

    def test_timeout_progress_write_failure_does_not_prevent_termination(self):
        import waa_compare
        original = waa_compare.write_json
        calls = []
        def failing_later(path, value):
            calls.append(value['status'])
            if len(calls) > 1:
                raise OSError('full evidence disk')
            original(path, value)
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            code = f"def cleanup(*_):\n pathlib.Path({str(folder / 'cleaned')!r}).touch()\n sys.exit(0)\nsignal.signal(signal.SIGTERM,cleanup)"
            with patch('waa_compare.write_json', side_effect=failing_later):
                self.run_ready_child(folder, code, 3)
            self.assertTrue((folder / 'cleaned').exists())
            self.assertEqual(calls, ['running', 'terminating', 'terminated_after_cleanup_grace'])


if __name__ == '__main__':
    unittest.main()
