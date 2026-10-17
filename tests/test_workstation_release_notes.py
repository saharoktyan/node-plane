import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('workstation_release_notes',
    ROOT / 'scripts/workstation_release_notes.py')
notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notes)


class WorkstationReleaseNotesTests(unittest.TestCase):
    def test_published_install_commands_use_pinned_channel_bootstrap_and_preserve_notes(self):
        body = ('## Changes\nKeep the actual changelog.\n\n'
                '## Install node-plane-cli 1.0.0-alpha.1\n'
                '### Install prebuilt binaries via shell script\n'
                'curl https://example.test/node-plane-cli-installer.sh | sh\n'
                '## Download node-plane-cli 1.0.0-alpha.1\nKeep download links.\n')
        for tag, channel, launcher in [('v1.0.0-alpha.1', 'dev', 'Node Plane (Dev)'),
                                       ('v1.0.0', 'stable', 'Node Plane')]:
            with self.subTest(channel=channel):
                result = notes.render_notes(body, tag)
                self.assertIn('Keep the actual changelog.', result)
                self.assertIn('Keep download links.', result)
                self.assertIn(f'## Install {launcher}\n', result)
                self.assertIn(f'node-plane-workstation-installer.sh | bash -s -- --channel {channel} --tag {tag}', result)
                self.assertIn(f"node-plane-workstation-installer.ps1'))) -Channel {channel} -Tag '{tag}'", result)
                self.assertNotIn('example.test', result)
                self.assertNotIn('node-plane-cli-installer.sh', result)

    def test_invalid_tag_cannot_change_notes_or_inject_commands(self):
        for tag in ('', '../dev', 'v1.0.0; sh', 'v1.0.0\n', 'v1.0.0-beta.1'):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                notes.render_notes('Changelog', tag)
