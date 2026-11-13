"""Package tracked source and the pinned official Python runtime; no user data."""
import hashlib
from pathlib import Path
import subprocess
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / 'dist'
PYTHON = 'python-3.12.10-embed-amd64.zip'
SHA256 = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'


def main():
    DIST.mkdir(exist_ok=True)
    archive = DIST / PYTHON
    if not archive.exists():
        urllib.request.urlretrieve('https://www.python.org/ftp/python/3.12.10/' + PYTHON, archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise ValueError('Python runtime checksum mismatch')
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    forbidden = {'accounts.json', 'preferences.json', 'session.bin', 'additional-titles.json',
                 'verified-titles-2026-11-06.json'}
    files = [Path(name) for name in tracked if name]
    for path in files:
        if path.name in forbidden or path.suffix.lower() in {'.sqlite', '.db', '.csv', '.exe', '.dll', '.log'} or set(path.parts) & {'outputs', 'dist', 'bin', 'obj'}:
            raise ValueError('Unexpected release input: ' + str(path))
    with zipfile.ZipFile(DIST / 'portable-payload.zip', 'w', zipfile.ZIP_DEFLATED) as out:
        with zipfile.ZipFile(archive) as runtime:
            for entry in runtime.infolist():
                if entry.filename != 'python312._pth':
                    out.writestr('python/' + entry.filename, runtime.read(entry))
        out.writestr('python/python312._pth', 'python312.zip\n.\n../studio\n../automation\n')
        for name in ('studio/worker.py', 'studio/run_stats.py', 'automation/xau_runner.py'):
            out.write(ROOT / name, name)
        for path in files:
            out.write(ROOT / path, 'source-code/' + path.as_posix())
            if path.parts[0] == 'LICENSES' or path.name in ('LICENSE', 'THIRD-PARTY-NOTICES.md'):
                out.write(ROOT / path, path.as_posix())
    print('Payload created without accounts, progress or a title catalogue.')


if __name__ == '__main__':
    main()
