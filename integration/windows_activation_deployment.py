"""Explicit legacy-image compatibility: replace an already registered activation worker."""
from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace


def parse_windows_command_line(command_line):
    from ctypes import wintypes
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    count = ctypes.c_int()
    argv = shell.CommandLineToArgvW(command_line, ctypes.byref(count))
    if not argv:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [argv[index] for index in range(count.value)]
    finally:
        kernel.LocalFree(argv)


def python_processes():
    # Use the workstation's built-in process inventory, not an optional Python
    # package. Return only parsed metadata; never print arbitrary command lines.
    script = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); @(Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%.exe'\" | Select-Object ProcessId,CommandLine) | ConvertTo-Json -Compress"
    raw = subprocess.check_output(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                  text=True, encoding='utf-8', errors='strict', timeout=10)
    rows = json.loads(raw) if raw.strip() else []
    if isinstance(rows, dict):
        rows = [rows]
    result = []
    for row in rows:
        if not isinstance(row.get('CommandLine'), str):
            raise RuntimeError('Could not inspect a Python process command line')
        result.append(SimpleNamespace(info={'pid': row['ProcessId'],
                      'cmdline': parse_windows_command_line(row['CommandLine'])}))
    return result


def load_worker(path):
    # Load fresh source directly; a copied .pyc must not supply the revision.
    namespace = {'__name__': 'waa_worker_validation', '__file__': str(path)}
    exec(compile(path.read_text(encoding='utf-8'), str(path), 'exec'), namespace)
    return namespace


def deploy(source, server_dir, expected_revision, processes):
    source, server_dir = Path(source), Path(server_dir)
    target = server_dir / 'windows_window_activation.py'
    candidate = load_worker(source)
    if candidate.get('CHANGE_ID') != expected_revision:
        raise RuntimeError('Activation worker revision does not match selected runtime')
    main = (server_dir / 'main.py').read_text(encoding='utf-8')
    marker = '# ' + candidate['INSTALL_MARKER']
    for required in [marker, 'from windows_window_activation import install as _waa_install_activation',
                     '_waa_install_activation(app)']:
        if main.count(required) != 1:
            raise RuntimeError('Baseline controller lacks the expected single worker registration')
    # The registered parent only supervises this path. Do not replace a worker
    # that belongs to an activation already in progress.
    for process in processes:
        argv = process.info.get('cmdline') or []
        if '--worker' in argv and any(Path(arg).name.lower() == target.name.lower() for arg in argv):
            raise RuntimeError('Activation worker is running; fresh bootstrap is not quiescent')
    previous = load_worker(target).get('CHANGE_ID') if target.exists() else None
    # The upload directory and server may be on different Windows volumes.
    # Stage beside the target so publication stays an atomic same-volume move.
    staged = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.activation-', suffix='.py', dir=server_dir, delete=False) as stream:
            staged = Path(stream.name)
            stream.write(source.read_bytes())
        os.replace(staged, target)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
    current = load_worker(target).get('CHANGE_ID')
    if current != expected_revision:
        raise RuntimeError('Installed activation worker revision differs from the selected runtime')
    return {'operation': 'legacy_activation_worker_replacement',
            'worker_change_id': current, 'previous_worker_change_id': previous,
            'worker_path': str(target), 'existing_controller_hook': True,
            'controller_reload': 'expected_on_source_replacement', 'deployment': 'atomic_worker_replacement'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--server-dir', type=Path, required=True)
    parser.add_argument('--expected-revision', required=True)
    args = parser.parse_args()
    result = deploy(args.source, args.server_dir, args.expected_revision, python_processes())
    print(json.dumps(result))


if __name__ == '__main__':
    main()
