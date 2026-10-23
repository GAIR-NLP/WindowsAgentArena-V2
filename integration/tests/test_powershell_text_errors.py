"""Exercise native PowerShell text streams when a canary executable is selected."""
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from guest_mcp_bridge import Session


@unittest.skipUnless(os.environ.get('WAA_TEST_PWSH'), 'Set WAA_TEST_PWSH to an explicit PowerShell canary executable')
class PowerShellTextTests(unittest.TestCase):
    def run_script(self, script):
        session = Session.__new__(Session)
        session.powershell_executable = str(Path(os.environ['WAA_TEST_PWSH']).resolve())
        session.command_lock = threading.Lock()
        session.failed = False
        session.command = None
        session.shutdown_error = None
        result = session._run_powershell(script, 15)
        self.assertFalse(result['timed_out'])
        self.assertFalse(result['cancelled'])
        self.assertEqual(result['effect'], 'not_verified')
        return result

    def assert_plain_diagnostic(self, text):
        self.assertNotIn('#< CLIXML', text)
        self.assertNotIn('<Objs', text)
        self.assertNotIn('\x1b', text)
        self.assertNotIn('_x000D_', text)

    def test_invalid_heredoc_remains_parser_error_before_any_script_runs(self):
        result = self.run_script("Write-Output 'must-not-run'\npython - <<'PY'\n")
        self.assertEqual(result['returncode'], 1)
        self.assertEqual(result['output'], '')
        self.assertIn('ParserError', result['error'])
        self.assertIn('Missing file specification after redirection operator.', result['error'])
        self.assertIn('python -', result['error'])
        self.assert_plain_diagnostic(result['error'])

    def test_unicode_streams_and_explicit_exit_status_are_preserved(self):
        result = self.run_script("[Console]::Out.WriteLine('stdout 中文 café'); [Console]::Error.WriteLine('stderr 日本語 Ω'); exit 7")
        self.assertEqual(result['returncode'], 7)
        self.assertIn('stdout 中文 café', result['output'])
        self.assertIn('stderr 日本語 Ω', result['error'])
        self.assertNotIn('stderr', result['output'])
        self.assertNotIn('stdout', result['error'])

    def test_nonterminating_error_does_not_replace_explicit_success_exit(self):
        result = self.run_script("Write-Error 'neutral nonterminating error'; Write-Output 'continued'; exit 0")
        self.assertEqual(result['returncode'], 0)
        self.assertIn('continued', result['output'])
        self.assertIn('neutral nonterminating error', result['error'])
        self.assert_plain_diagnostic(result['error'])

    def test_terminating_error_keeps_message_and_failure(self):
        result = self.run_script("throw 'neutral throw 中文'")
        self.assertEqual(result['returncode'], 1)
        self.assertIn('neutral throw 中文', result['error'])
        self.assert_plain_diagnostic(result['error'])

    def test_literal_xml_and_powershell_escape_text_are_not_decoded(self):
        result = self.run_script("Write-Output '#< CLIXML'; Write-Output '<S>_x000D_&lt;</S>'")
        self.assertEqual(result['returncode'], 0)
        self.assertIn('#< CLIXML', result['output'])
        self.assertIn('<S>_x000D_&lt;</S>', result['output'])
        self.assertEqual(result['error'], '')

    def test_color_selection_is_only_in_child_environment(self):
        with patch.dict(os.environ, {'NO_COLOR': 'parent-value', 'WAA_NEUTRAL_ENV': 'preserved'}):
            result = self.run_script("Write-Output $env:NO_COLOR; Write-Output $env:WAA_NEUTRAL_ENV")
            self.assertEqual(os.environ['NO_COLOR'], 'parent-value')
            self.assertEqual(os.environ['WAA_NEUTRAL_ENV'], 'preserved')
        self.assertEqual(result['returncode'], 0)
        self.assertIn('1', result['output'].splitlines())
        self.assertIn('preserved', result['output'].splitlines())


if __name__ == '__main__':
    unittest.main()
