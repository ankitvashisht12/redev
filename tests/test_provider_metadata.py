"""Readable Codespace names and passive status describe provider and app state separately."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from redev.config import ConfigError, validate_config
from redev.runner import command_environment
from redev.transport import GitHubTransport, TransportError
import test_check_workflow


class ProviderMetadataTests(unittest.TestCase):
    setUp = test_check_workflow.CheckWorkflowTests.setUp
    write_config = test_check_workflow.CheckWorkflowTests.write_config
    latest_result = test_check_workflow.CheckWorkflowTests.latest_result

    def test_creation_name_starts_with_pr_and_reuse_survives_display_rename(self):
        self.provider.creation_label = lambda root, branch=None: '496'
        self.assertEqual(self.app.check(['pass'], stop=True), 0)
        display = self.provider.environments[0]['displayName']
        self.assertTrue(display.startswith('496-'), display)
        self.assertLessEqual(len(display), 48)
        self.provider.environments[0]['displayName'] = 'renamed manually'
        self.assertEqual(self.app.check(['pass'], stop=True), 0)
        self.assertEqual(self.provider.creations, 1)

    def test_recovery_matches_stable_marker_despite_a_different_pr_prefix(self):
        self.provider.environments.append({'name': 'old-space', 'displayName': '123-redev-' + self.store.identity, 'state': 'Shutdown'})
        self.provider.creation_label = lambda root, branch=None: '496'
        self.assertEqual(self.app.check(['pass'], stop=True), 0)
        self.assertEqual(self.provider.creations, 0)
        self.assertEqual(self.store.read()['codespace'], 'old-space')

    def test_status_is_passive_and_exposes_billing_timeout_retention_and_readiness(self):
        self.app.check(['pass'], stop=True)
        self.provider.codespace_metadata = lambda name: {'displayName': '496-work', 'state': 'Shutdown',
            'repository': {'nameWithOwner': 'example/project'}, 'billableOwner': {'login': 'example-org'},
            'machineName': 'medium', 'machineDisplayName': '4 cores, 16 GB', 'idleTimeoutMinutes': 5,
            'retentionPeriodDays': 7, 'retentionExpiresAt': '2026-09-12T00:00:00Z', 'createdAt': '2026-09-05T00:00:00Z'}
        self.provider.connect = lambda *args, **kwargs: self.fail('status must not connect')
        status = self.app.status()
        self.assertEqual(status['billableOwner'], {'login': 'example-org'})
        self.assertEqual(status['idleTimeoutMinutes'], 5)
        self.assertEqual(status['retentionPeriodDays'], 7)
        self.assertEqual(status['machineDisplayName'], '4 cores, 16 GB')
        self.assertEqual(status['displayName'], '496-work')
        self.assertFalse(status['readiness']['checkedLive'])
        self.assertEqual(status['readiness']['services'], 'stopped')

    def test_available_provider_does_not_claim_apps_ready_without_setup(self):
        self.store.save({'enabled': True, 'codespace': 'starting-space', 'repository': 'example/project'})
        self.provider.environments.append({'name': 'starting-space', 'state': 'Available'})
        self.provider.codespace_metadata = lambda name: {'state': 'Available', 'displayName': 'creating'}
        status = self.app.status()
        self.assertEqual(status['readiness']['setup'], 'not observed')
        self.assertEqual(status['readiness']['services'], 'not observed')

    def test_configured_https_urls_agree_with_command_environment(self):
        self.config['ports'] = {'web': 3443}
        self.config['portSchemes'] = {'web': 'https'}
        self.write_config()
        with patch('redev.state.port_available', return_value=True):
            self.app.up(no_watch=True)
        status = self.app.status()
        self.assertEqual(status['urls']['web'], 'https://localhost:3443')
        self.assertEqual(command_environment({'web': 3443}, {'web': 'https'})['REDEV_URL_WEB'], status['urls']['web'])

    def test_human_status_includes_provider_details_without_claiming_live_readiness(self):
        from redev.__main__ import format_status
        output = format_status({'enabled': True, 'root': '/repo', 'repository': 'example/project',
            'displayName': '496-feature', 'codespace': 'fixture-space', 'codespaceState': 'Available',
            'machineDisplayName': '4 cores, 16 GB', 'billableOwner': {'login': 'example-org'},
            'idleTimeoutMinutes': 5, 'retentionPeriodDays': 7, 'retentionExpiresAt': 'next-week',
            'readiness': {'setup': 'not observed', 'services': 'not observed', 'checkedLive': False}})
        for value in ['example/project', '496-feature', '4 cores, 16 GB', 'example-org', '5 minutes', '7 days', 'next-week', 'not observed']:
            self.assertIn(value, output)


class NamingTransportTests(unittest.TestCase):
    def test_pr_number_wins_and_missing_pr_falls_back_to_branch(self):
        with tempfile.TemporaryDirectory() as temporary:
            transport = GitHubTransport(Path(temporary))
            def capture(args):
                if args[:3] == ['git', '-C', str(Path(temporary))]:
                    return 'feature/detailed-branch'
                if args[:3] == ['gh', 'pr', 'view']:
                    return '42'
                raise AssertionError(args)
            with patch.object(transport, 'capture', capture):
                self.assertEqual(transport.creation_label(Path(temporary)), '42')
            def no_pr(args):
                if args[0] == 'git':
                    return 'feature/detailed-branch'
                raise TransportError('no pull request')
            with patch.object(transport, 'capture', no_pr):
                self.assertEqual(transport.creation_label(Path(temporary)), 'detailed-branch')

    def test_port_schemes_reject_unknown_names_and_unsafe_protocols(self):
        for schemes in [{'other': 'https'}, {'web': 'file'}, {'web': ['https']}]:
            with self.subTest(schemes=schemes), self.assertRaises(ConfigError):
                validate_config({'version': 1, 'checks': {'test': 'true'}, 'ports': {'web': 3443}, 'portSchemes': schemes})


if __name__ == '__main__':
    unittest.main()
