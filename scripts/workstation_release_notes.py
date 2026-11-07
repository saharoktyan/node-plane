"""Use channel-aware, tag-pinned workstation bootstrap commands in release notes."""
import argparse
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from services.release_versions import release_channel
from services.release_changelog import version_changes


def render_notes(body, tag, changelog=None):
    if not re.fullmatch(r'v\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?', tag):
        raise ValueError('Invalid release tag')
    if changelog is not None:
        changes = version_changes(changelog, tag)
        if changes:
            body = re.sub(r"(?ms)^## (?:Changes|What's Changed|Changelog|Release notes)[ \t]*\n.*?(?=^## |\Z)", '', body)
            body = '## Changes\n\n' + changes + '\n\n' + body
    channel = release_channel(tag)
    name = {'dev': 'Node Plane (Dev)', 'beta': 'Node Plane (Beta)', 'stable': 'Node Plane'}[channel]
    base = f'https://github.com/saharoktyan/node-plane/releases/download/{tag}'
    install = f'''## Install {name}

### Linux and macOS

```sh
curl --proto '=https' --tlsv1.2 -LsSf {base}/node-plane-workstation-installer.sh | bash -s -- --channel {channel} --tag {tag}
```

### Windows PowerShell

```powershell
& ([scriptblock]::Create((irm '{base}/node-plane-workstation-installer.ps1'))) -Channel {channel} -Tag '{tag}'
```

'''
    # Preserve changelog/download sections; replace cargo-dist's generic install
    # section, whose raw installer does not create our channel-specific launchers.
    pattern = r'(?ms)^## Install node-plane-cli[^\n]*\n.*?(?=^## |\Z)'
    if re.search(pattern, body):
        return re.sub(pattern, lambda _match: install, body, count=1)
    return install + body


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tag', required=True)
    parser.add_argument('--notes', type=Path, required=True)
    args = parser.parse_args()
    changelog = Path(__file__).resolve().parents[1] / 'CHANGELOG.md'
    args.notes.write_text(render_notes(args.notes.read_text(), args.tag,
        changelog.read_text() if changelog.is_file() else None))


if __name__ == '__main__':
    main()
