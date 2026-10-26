"""Use channel-aware, tag-pinned workstation bootstrap commands in release notes."""
import argparse
from pathlib import Path
import re


def render_notes(body, tag):
    if not re.fullmatch(r'v\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?', tag):
        raise ValueError('Invalid release tag')
    channel = 'dev' if re.search(r'-(?:alpha|beta|rc)\.\d+$', tag) else 'stable'
    name = 'Node Plane (Dev)' if channel == 'dev' else 'Node Plane'
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
    args.notes.write_text(render_notes(args.notes.read_text(), args.tag))


if __name__ == '__main__':
    main()
