#!/usr/bin/env python3
"""Install a launcher for this checkout without pip or shell configuration edits."""
import argparse
import os
from pathlib import Path
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir', default=str(Path.home() / '.local' / 'bin'))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    destination = Path(args.bin_dir).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / 'localcloud'
    if target.exists():
        existing = target.read_text()
        if '# LocalCloud checkout launcher' not in existing:
            parser.error(f'{target} already exists and is not our launcher; choose another --bin-dir')
    script = ('#!/usr/bin/env python3\n# LocalCloud checkout launcher\nimport sys\n'
              f'sys.path.insert(0, {str(root)!r})\nfrom localcloud.cli import main\nraise SystemExit(main())\n')
    descriptor, temporary = tempfile.mkstemp(prefix='.localcloud-', dir=destination)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(script)
        os.chmod(temporary, 0o755)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(f'Installed {target}')
    print('Keep this checkout in place; the launcher imports its code directly.')
    print('If localcloud is not found, add the installation directory to your PATH.')


if __name__ == '__main__':
    main()
