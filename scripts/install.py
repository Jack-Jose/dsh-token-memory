"""Install only the distributable skill files; preserve local root configuration."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import sys
import zipfile

FILES = (
    'SKILL.md', 'README.md', 'LICENSE', '.gitignore', 'memory.config.example.json',
    'references/usage.md', 'examples/record.json', 'examples/checkpoint.json',
    'scripts/memory.py', 'scripts/test_memory.py', 'scripts/install.py',
    '.github/workflows/tests.yml',
)


def install(target=None, upgrade=False):
    source = Path(__file__).resolve().parents[1]
    dsh_home = os.environ.get('DSH_HOME')
    host = Path(os.path.expandvars(dsh_home)).expanduser() if dsh_home else Path.home() / '.dsh'
    destination = Path(target).expanduser().resolve() if target else (host / 'skills' / 'dsh-token-memory').resolve()
    if destination == source:
        return {'status': 'already_installed', 'target': str(destination)}
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError('Source and target must not contain each other')
    for name in FILES:
        if not (source / name).is_file():
            raise ValueError('Distribution file missing: ' + name)
    backup = None
    if destination.exists():
        if not upgrade:
            raise ValueError('Target exists; use --upgrade to back up and update')
        identity = destination / 'SKILL.md'
        if not identity.is_file() or 'name: dsh-token-memory' not in identity.read_text(encoding='utf-8'):
            raise ValueError('Target is not an existing dsh-token-memory skill')
        now = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = destination.parent / ('dsh-token-memory-backup-' + now + '.zip')
        with zipfile.ZipFile(backup, 'x', zipfile.ZIP_DEFLATED) as archive:
            for name in FILES + ('memory.config.json',):
                item = destination / name
                if item.is_file():
                    archive.write(item, arcname='dsh-token-memory/' + name)
    destination.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        output = destination / name
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, output)
    return {'status': 'installed', 'target': str(destination), 'backup': str(backup) if backup else None,
            'note': 'Restart/refresh the host skill list; no compaction hook was installed.'}


if __name__ == '__main__':
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', help='Full destination skill directory')
    parser.add_argument('--upgrade', action='store_true')
    args = parser.parse_args()
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8')
        print(json.dumps(install(args.target, args.upgrade), ensure_ascii=False))
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
