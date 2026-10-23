"""Bootstrap waits for the new controller without replaying deployment."""
import http.client
import unittest
from unittest.mock import patch

from vm import deploy_activation_worker


class ReloadTests(unittest.TestCase):
    old = {'controller_pid': 10, 'reloader': 'true', 'worker_change_id': 'old'}
    new = {'controller_pid': 11, 'reloader': 'true', 'worker_change_id': 'new'}

    def test_lost_mutation_reply_is_confirmed_without_replay(self):
        with patch('vm.activation_controller_state', side_effect=[self.old, http.client.RemoteDisconnected(), self.old, self.new]), \
                patch('vm.execute', side_effect=http.client.RemoteDisconnected()) as mutate, \
                patch('vm.time.sleep'):
            result = deploy_activation_worker('url', ['deploy'], 'new')
        self.assertTrue(result['deployment_request_interrupted'])
        self.assertEqual(result['after'], self.new)
        mutate.assert_called_once()

    def test_successful_reply_still_waits_for_new_process_and_revision(self):
        wrong = dict(self.new, worker_change_id='old')
        unchanged_pid = dict(self.old, worker_change_id='new')
        with patch('vm.activation_controller_state', side_effect=[self.old, unchanged_pid, wrong, self.new]), \
                patch('vm.execute'), patch('vm.time.sleep'):
            result = deploy_activation_worker('url', ['deploy'], 'new')
        self.assertEqual(len(result['observations']), 3)

    def test_unchanged_process_times_out(self):
        with patch('vm.activation_controller_state', return_value=self.old), patch('vm.execute'), \
                patch('vm.time.monotonic', side_effect=[0, 0, 31]), patch('vm.time.sleep'):
            with self.assertRaisesRegex(RuntimeError, 'within 30 seconds'):
                deploy_activation_worker('url', ['deploy'], 'new')

    def test_semantic_deployment_failure_is_not_swallowed(self):
        with patch('vm.activation_controller_state', return_value=self.old), \
                patch('vm.execute', side_effect=RuntimeError('worker validation failed')):
            with self.assertRaisesRegex(RuntimeError, 'worker validation failed'):
                deploy_activation_worker('url', ['deploy'], 'new')

    def test_unknown_controller_does_not_mutate(self):
        with patch('vm.activation_controller_state', return_value=dict(self.old, controller_pid=None)), \
                patch('vm.execute') as mutate:
            with self.assertRaisesRegex(RuntimeError, 'baseline controller'):
                deploy_activation_worker('url', ['deploy'], 'new')
        mutate.assert_not_called()

    def test_no_reloader_confirms_worker_without_requiring_new_process(self):
        before = dict(self.old, reloader=None)
        after = dict(before, worker_change_id='new')
        with patch('vm.activation_controller_state', side_effect=[before, after]), patch('vm.execute'):
            result = deploy_activation_worker('url', ['deploy'], 'new')
        self.assertFalse(result['controller_restarted'])
