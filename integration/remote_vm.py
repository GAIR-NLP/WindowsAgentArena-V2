"""Run one WAA episode over an episode-owned SSH connection and loopback forwards."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import time

PORTS = (5000, 8765, 9222, 8080)
ROOT = Path(os.environ.get('WAA_ROOT', str(Path.home() / '.local/share/waa-v2')))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def save_local(path, binding):
    save(path, binding)
    if os.environ.get('WAA_RESET_RECORD_FILE'):
        save(Path(os.environ['WAA_RESET_RECORD_FILE']), binding)


def ssh_command(host, argv):
    if not host or host.startswith('-'):
        raise ValueError('Invalid SSH host')
    return ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', host, shlex.join(argv)]


def remote_command(binding, action):
    return ssh_command(binding['ssh_host'], [
        'env', 'WAA_ROOT=' + binding['remote_root'], binding['remote_python'],
        binding['remote_script'], action, '--worker-record', binding['worker_record'],
    ])


def start_ticks(pid):
    # comm may contain spaces or parentheses; fields after the final ')' start at field 3.
    return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]


def process_matches(info):
    try:
        return start_ticks(info['pid']) == info['start_ticks']
    except (FileNotFoundError, ProcessLookupError):
        return False


def signal_worker(info):
    # Pin the process before checking its identity so PID reuse cannot target another process.
    try:
        descriptor = os.pidfd_open(info['pid'])
    except ProcessLookupError:
        return
    try:
        if process_matches(info):
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGTERM)
            except ProcessLookupError:
                pass  # The pinned worker exited after the identity check.
    finally:
        os.close(descriptor)


def worker(args):
    import vm
    record = args.worker_record
    record.parent.mkdir(parents=True, exist_ok=True)
    info = {'pid': os.getpid(), 'start_ticks': start_ticks(os.getpid()), 'status': 'starting'}
    cancel = record.with_suffix('.cancel')
    progress = record.with_suffix('.vm.json')
    def interrupted(signum, _frame):
        raise SystemExit(f'Remote reset interrupted by signal {signum}')
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, interrupted)
    # Claim exclusively, then publish complete JSON atomically for concurrent cancellation.
    record.with_suffix('.claim').mkdir(exist_ok=False)
    save(record, info)
    try:
        if cancel.exists():
            raise SystemExit('Episode cancelled before reset')
        os.environ['WAA_RESET_RECORD_FILE'] = str(progress)
        options = json.loads(args.reset_options)
        options['snapshot'] = Path(options['snapshot'])
        options['hcu_archive'] = Path(options['hcu_archive'])
        for field in ('cua_archive', 'guest_driver_config', 'bridge_script', 'activation_worker'):
            if options.get(field) is not None:
                options[field] = Path(options[field])
        vm.start(argparse.Namespace(**options))
    finally:
        info['status'] = 'finished'
        save(record, info)


def cancel_worker(args):
    import vm
    record = args.worker_record
    record.parent.mkdir(parents=True, exist_ok=True)
    record.with_suffix('.cancel').touch(exist_ok=True)
    if record.exists():
        info = json.loads(record.read_text())
        if info['status'] != 'finished' and process_matches(info):
            signal_worker(info)
            until = time.monotonic() + 220
            while process_matches(info) and time.monotonic() < until:
                if json.loads(record.read_text()).get('status') == 'finished':
                    break
                time.sleep(0.5)
            else:
                if process_matches(info):
                    raise TimeoutError('Remote reset has not finished graceful cleanup')
    progress = record.with_suffix('.vm.json')
    if progress.exists():
        binding = json.loads(progress.read_text())
        result = vm.capture_and_stop(binding, Path(binding['storage']).parent)
        vm.save_binding(Path(binding['storage']).parent, binding)
        save(progress, binding)
    else:
        result = {'status': 'cancelled_before_container'}
    print(json.dumps(result), flush=True)


def check_ports(address):
    if not ipaddress.ip_address(address).is_loopback or ':' in address:
        raise ValueError('Forward address must be an IPv4 loopback address')
    sockets = []
    try:
        for port in PORTS:
            sock = socket.socket()
            sockets.append(sock)
            # Match SSH forwarding: reuse closed connections in TIME_WAIT,
            # while an existing listener still makes bind fail.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((address, port))
    finally:
        for sock in sockets:
            sock.close()


def tunnel_command(binding):
    command = ['ssh', '-M', '-S', binding['control_path'], '-o', 'ControlPersist=no',
               '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
               '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
               '-o', 'ExitOnForwardFailure=yes', '-N', '-f']
    remote_ip = str(ipaddress.ip_address(binding['remote_binding']['vm_ip']))
    for port in PORTS:
        command += ['-L', f"{binding['vm_ip']}:{port}:{remote_ip}:{port}"]
    command.append(binding['ssh_host'])
    return command


def stop(binding, path):
    results = {}
    try:
        result = subprocess.run(remote_command(binding, 'cancel'), capture_output=True, text=True, timeout=280)
        results['remote'] = {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
    except Exception as exc:
        results['remote'] = {'error': str(exc)}
    try:
        control = Path(binding['control_path'])
        if control.exists():
            result = subprocess.run(['ssh', '-S', str(control), '-O', 'exit', binding['ssh_host']],
                                    capture_output=True, text=True, timeout=20)
            results['tunnel'] = {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
        else:
            results['tunnel'] = {'returncode': 0, 'status': 'absent'}
    except Exception as exc:
        results['tunnel'] = {'error': str(exc)}
    binding['cleanup'] = results
    save_local(path, binding)
    if any(value.get('returncode') != 0 for value in results.values()):
        raise RuntimeError('Remote episode cleanup failed; see ' + str(path))
    return results


def reset(args):
    check_ports(args.loopback_ip)
    episode = secrets.token_hex(12)
    directory = ROOT / 'remote-episodes' / episode
    directory.mkdir(parents=True, exist_ok=False)
    path = directory / 'episode.json'
    # A short, private directory keeps ControlPath below the Unix socket path limit.
    control_dir = Path('/tmp') / ('waa-ssh-' + episode)
    control_dir.mkdir(mode=0o700)
    binding = {'episode_id': episode, 'status': 'creating', 'ssh_host': args.ssh_host,
               'remote_root': args.remote_root, 'remote_python': args.remote_python,
               'remote_script': str(Path(args.remote_repo) / 'integration/remote_vm.py'),
               'worker_record': str(Path(args.remote_root) / 'remote-workers' / (episode + '.json')),
               'control_path': str(control_dir / 'control'), 'vm_ip': args.loopback_ip,
               'controller_url': f'http://{args.loopback_ip}:5000',
               'bridge_url': f'http://{args.loopback_ip}:8765',
               'cleanup_command': [sys.executable, str(Path(__file__).resolve()), 'stop', '--episode', str(path)]}
    save_local(path, binding)
    options = {'run_id': args.run_id, 'arm': args.arm, 'boot_timeout': args.boot_timeout,
               'hyperv': 'N', 'snapshot': args.snapshot,
               'hcu_archive': args.hcu_archive, 'cua_archive': getattr(args, 'cua_archive', None),
               'guest_driver_config': getattr(args, 'guest_driver_config', None),
               'lock_network': args.lock_network}
    for field in ('image', 'network', 'timezone', 'windows_timezone', 'bridge_script',
                  'activation_worker', 'activation_revision', 'activation_server_dir'):
        if hasattr(args, field):
            options[field] = getattr(args, field)
    command = remote_command(binding, 'worker')
    command[-1] += ' ' + shlex.join(['--reset-options', json.dumps(options)])
    process = None
    try:
        with (directory / 'reset.stdout').open('w') as out, (directory / 'reset.stderr').open('w') as err:
            process = subprocess.Popen(command, stdout=out, stderr=err)
            returncode = process.wait(timeout=args.reset_timeout)
        if returncode:
            raise RuntimeError('Remote VM reset failed; see ' + str(directory / 'reset.stderr'))
        remote_binding = json.loads((directory / 'reset.stdout').read_text())
        if remote_binding.get('status') != 'ready':
            raise RuntimeError('Remote VM did not return a ready binding')
        binding['remote_binding'] = remote_binding
        save_local(path, binding)
        subprocess.run(tunnel_command(binding), check=True, capture_output=True, text=True, timeout=30)
        binding['status'] = 'ready'
        save_local(path, binding)
        print(json.dumps(binding), flush=True)
    except BaseException as exc:
        binding.update(status='failed', failure=f'{type(exc).__name__}: {exc}')
        try:
            save_local(path, binding)
        except Exception as record_error:
            exc.add_note(f'Could not save failed remote binding: {record_error}')
        try:
            stop(binding, path)
        except Exception as cleanup_error:
            exc.add_note(f'Remote episode cleanup failed: {cleanup_error}')
        raise
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # This is the still-owned local SSH child, never a reused numeric PID.
                process.kill()
                process.wait()


def main():
    global ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['reset', 'stop', 'worker', 'cancel'])
    parser.add_argument('--episode', type=Path)
    parser.add_argument('--worker-record', type=Path)
    parser.add_argument('--reset-options')
    parser.add_argument('--root', type=Path, default=ROOT, help='Local owner state directory')
    parser.add_argument('--ssh-host')
    parser.add_argument('--remote-root', help='Absolute owner state path on the VM host')
    parser.add_argument('--remote-repo', help='Absolute repository path on the VM host')
    parser.add_argument('--remote-python', default='python3')
    parser.add_argument('--loopback-ip', default='127.0.0.1')
    parser.add_argument('--run-id')
    parser.add_argument('--image', help='Prepared Windows container image tag on the VM host')
    parser.add_argument('--network', help='Existing Docker network on the VM host')
    parser.add_argument('--timezone', default='UTC')
    parser.add_argument('--windows-timezone', default='UTC')
    parser.add_argument('--arm', choices=['hcu', 'cua'], default='hcu')
    parser.add_argument('--snapshot')
    parser.add_argument('--hcu-archive')
    parser.add_argument('--cua-archive', help='Explicit CUA Windows ZIP on the remote host')
    parser.add_argument('--guest-driver-config', help='Developer JSON driver configuration on the remote host')
    parser.add_argument('--bridge-script', help='Bridge source path on the VM host')
    parser.add_argument('--activation-worker', help='Opt-in legacy activation worker source on the VM host')
    parser.add_argument('--activation-revision')
    parser.add_argument('--activation-server-dir', default=r'C:\OEM\server')
    parser.add_argument('--boot-timeout', type=int, default=600)
    parser.add_argument('--reset-timeout', type=int, default=1800)
    parser.add_argument('--lock-network', action='store_true')
    args = parser.parse_args()
    ROOT = args.root.resolve()
    if args.boot_timeout <= 0 or args.reset_timeout <= 0:
        parser.error('boot-timeout and reset-timeout must be positive')
    if args.action == 'worker':
        if not args.worker_record or not args.reset_options:
            parser.error('worker requires --worker-record and --reset-options')
        worker(args)
    elif args.action == 'cancel':
        if not args.worker_record:
            parser.error('cancel requires --worker-record')
        cancel_worker(args)
    elif args.action == 'stop':
        if not args.episode:
            parser.error('stop requires --episode')
        print(json.dumps(stop(json.loads(args.episode.read_text()), args.episode)))
    else:
        for field in ('ssh_host', 'remote_root', 'remote_repo', 'run_id', 'image', 'network',
                      'snapshot', 'hcu_archive', 'cua_archive'):
            if not getattr(args, field):
                parser.error('reset requires --' + field.replace('_', '-'))
        for field in ('remote_root', 'remote_repo', 'snapshot', 'hcu_archive', 'cua_archive',
                      'guest_driver_config', 'bridge_script', 'activation_worker'):
            value = getattr(args, field)
            if value is not None and not Path(value).is_absolute():
                parser.error('--' + field.replace('_', '-') + ' must be an absolute remote path')
        def interrupted(signum, _frame):
            raise SystemExit(f'Remote wrapper interrupted by signal {signum}')
        signal.signal(signal.SIGTERM, interrupted)
        reset(args)


if __name__ == '__main__':
    main()
