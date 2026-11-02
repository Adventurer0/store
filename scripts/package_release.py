"""Build a source release without local dependencies, credentials or store data."""
from pathlib import Path
import hashlib
import json
import zipfile


def main():
    root = Path(__file__).resolve().parents[1]
    version = json.loads((root / 'package.json').read_text(encoding='utf-8'))['version']
    target = root.parent / f'ShopDock_Local_Docker_v{version}.zip'
    files = [
        '.dockerignore', '.env.example', '.gitattributes', '.gitignore',
        'Dockerfile', 'LICENSE', 'README.md', 'THIRD_PARTY_NOTICES.md',
        'compose.yaml', 'compose.core.yaml', 'package.json', 'package-lock.json',
        'requirements.txt', 'requirements-dev.txt', 'start-local.bat',
        'start-windows.bat', 'start.sh',
    ]
    paths = [root / name for name in files]
    for name in ('app', 'docs', 'examples', 'scripts', 'tests'):
        paths.extend(p for p in (root / name).rglob('*')
                     if p.is_file() and '__pycache__' not in p.parts
                     and p.suffix not in ('.pyc', '.pyo'))
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(paths):
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError('Release path escapes the source directory')
            archive.write(path, 'shopdock/' + path.relative_to(root).as_posix())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix('.sha256').write_text(f'{digest}  {target.name}\n', encoding='ascii')
    with zipfile.ZipFile(target) as archive:
        if archive.testzip() is not None:
            raise ValueError('Archive integrity verification failed')
    print(f'{target.name}: {len(paths)} files, {target.stat().st_size} bytes')
    print(f'SHA256 {digest}')


if __name__ == '__main__':
    main()
