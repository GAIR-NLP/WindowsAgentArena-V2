"""Own fresh WAA Windows guests and install only the two driver runtimes."""
from __future__ import annotations

import argparse
import datetime
import http.client
import json
import os
from pathlib import Path, PureWindowsPath
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(os.environ.get('WAA_ROOT', str(Path.home() / '.local/share/waa-v2')))
REPO = Path(__file__).resolve().parents[1]
AUDIO_CAPTURE_ARGS = (
    '-audiodev', 'none,id=waa-audio,out.frequency=44100,out.channels=2,out.format=s16',
    '-device', 'ich9-intel-hda,id=waa-hda',
    '-device', 'hda-output,bus=waa-hda.0,audiodev=waa-audio',
)
# The supported container uses legacy IPv4 and nft IPv6 filter tables.
# Both firewall commands run in the container network namespace.
NETWORK_LOCK = r'''#!/usr/bin/env bash
set -Eeuo pipefail
[[ "${NETWORK,,}" != user* && "${DHCP,,}" == n* ]] || { echo 'WAA requires NAT networking for egress isolation' >&2; exit 1; }
for firewall in iptables-legacy ip6tables-nft; do
    "$firewall" -N WAA_EGRESS
    "$firewall" -A WAA_EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
    "$firewall" -A WAA_EGRESS -j REJECT
    "$firewall" -I FORWARD 1 -i dockerbridge -o "$VM_NET_DEV" -j WAA_EGRESS
    "$firewall" -I INPUT 1 -i dockerbridge -p udp --dport 53 -j REJECT
    "$firewall" -I INPUT 1 -i dockerbridge -p tcp --dport 53 -j REJECT
    "$firewall" -C FORWARD -i dockerbridge -o "$VM_NET_DEV" -j WAA_EGRESS
    "$firewall" -C WAA_EGRESS -j REJECT
done
printf '%s\n' "$VM_NET_DEV" > /run/waa-network-locked
'''
LOCK_ENTRYPOINT = r'''set -Eeuo pipefail
awk '/^[.] network[.]sh([[:space:]]|$)/ { print; print ". /waa-network-lock.sh"; found++; next } { print } END { if (found != 1) exit 1 }' /run/entry.sh > /run/waa-entry.sh
chmod 700 /run/waa-entry.sh
exec /usr/bin/tini -s /run/waa-entry.sh
'''
DRIVER_INSTALL = r'''
import zipfile, shutil
for archive, folder, executable in [('hcu.zip','hcu','computer-use.exe'),('cua.zip','cua-driver','cua-driver.exe')]:
    dest=p/folder
    # A prepared baseline may contain an older flattened driver installation.
    # Replace only this fresh episode's driver directory before unpacking.
    if dest.exists(): shutil.rmtree(dest)
    dest.mkdir()
    with zipfile.ZipFile(p/archive) as z: z.extractall(dest)
    exe=list(dest.rglob(executable))
    if len(exe)!=1: raise RuntimeError('Expected exactly one '+executable)
    if exe[0].parent != dest:
        for item in list(exe[0].parent.iterdir()): shutil.move(str(item),str(dest/item.name))
'''


def command(args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=kwargs.pop('timeout', 120), **kwargs).stdout.strip()


def request(url, data=None, timeout=120, headers=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                                 headers={'Content-Type': 'application/json', **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def upload(url, source, target):
    boundary = 'waa-upload-' + secrets.token_hex(12)
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file_path"\r\n\r\n{target}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file_data"; filename="{source.name}"\r\n'
            'Content-Type: application/octet-stream\r\n\r\n').encode() + source.read_bytes() + f'\r\n--{boundary}--\r\n'.encode()
    req = urllib.request.Request(url + '/setup/upload', data=body,
                                 headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
    with urllib.request.urlopen(req, timeout=180) as response:
        result = response.read().decode('utf-8')
    return result


def execute(url, code):
    result = request(url + '/execute', {'command': ['python', '-c', code], 'shell': False})
    if result.get('returncode') != 0:
        raise RuntimeError('Guest command failed: ' + str(result))
    return result


def activation_controller_state(url, server_dir=r'C:\OEM\server'):
    code = r'''import json,os
from pathlib import Path
''' + 'p=Path(' + repr(str(PureWindowsPath(server_dir) / 'windows_window_activation.py')) + ')' + r'''
n={'__name__':'waa_worker_validation','__file__':str(p)}
exec(compile(p.read_text(encoding='utf-8'),str(p),'exec'),n)
print(json.dumps({'controller_pid':os.getppid(),'reloader':os.environ.get('WERKZEUG_RUN_MAIN'),
                  'worker_change_id':n['CHANGE_ID']}))'''
    result = request(url + '/execute', {'command': ['python', '-c', code], 'shell': False}, timeout=2)
    if result.get('returncode') != 0:
        raise RuntimeError('Could not inspect activation controller state')
    return json.loads(result['output'])


def deploy_activation_worker(url, activation_command, expected_revision, server_dir=r'C:\OEM\server'):
    before = activation_controller_state(url, server_dir)
    if before.get('reloader') not in (None, 'true') or not isinstance(before.get('controller_pid'), int):
        raise RuntimeError('Unknown baseline controller state before worker deployment')
    needs_reload = before.get('reloader') == 'true'
    transport_errors = (urllib.error.URLError, http.client.RemoteDisconnected, ConnectionError, TimeoutError)
    interrupted = False
    try:
        execute(url, 'import subprocess,sys; subprocess.run([sys.executable] + '
                + repr(activation_command) + ', check=True, timeout=15)')
    except transport_errors:
        # Source replacement can restart Flask before its deployment request
        # returns. Never repeat the mutation: confirm the new process instead.
        interrupted = True
    deadline = time.monotonic() + 30
    observations = []
    while time.monotonic() < deadline:
        try:
            after = activation_controller_state(url, server_dir)
        except transport_errors:
            after = None
        observations.append(after)
        if (after and isinstance(after.get('controller_pid'), int)
                and (not needs_reload or after['controller_pid'] != before['controller_pid'])
                and after.get('reloader') == before.get('reloader')
                and after.get('worker_change_id') == expected_revision):
            return {'operation': 'activation_worker_deployment',
                    'before': before, 'after': after,
                    'controller_restarted': after['controller_pid'] != before['controller_pid'],
                    'deployment_request_interrupted': interrupted, 'observations': observations}
        time.sleep(.25)
    raise RuntimeError('Controller did not confirm the selected activation worker within 30 seconds')


def token_path():
    path = ROOT / 'operations/bridge-token.txt'
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        fd, staged = tempfile.mkstemp(prefix='.bridge-token-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(secrets.token_urlsafe(36))
            # Publish only a complete token. Concurrent creators keep the first
            # complete file without exposing an empty destination or overwriting it.
            try:
                os.link(staged, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(staged)
    if not path.read_text().strip():
        raise RuntimeError('Bridge token file is empty: ' + str(path))
    return path


def resolve_cua_archive(archive=None):
    if archive is None:
        raise ValueError('An explicit CUA Windows ZIP is required')
    archive = Path(archive).resolve()
    if not archive.is_file():
        raise FileNotFoundError('CUA archive is missing: ' + str(archive))
    return archive


def load_guest_driver_config(path=None):
    config = json.loads(Path(path).read_text()) if path is not None else {
        'arms': {'hcu': {'command': [r'C:\waa-bridge\hcu\computer-use.exe', 'mcp']},
                 'cua': {'command': [r'C:\waa-bridge\cua-driver\cua-driver.exe', 'mcp']}}}
    if not isinstance(config, dict) or not isinstance(config.get('arms'), dict) or set(config['arms']) != {'hcu', 'cua'}:
        raise ValueError('Guest driver config requires both hcu and cua arms')
    for name, arm in config['arms'].items():
        if not isinstance(arm, dict):
            raise ValueError('Guest driver arm must be an object: ' + name)
        command = arm.get('command')
        if (not isinstance(command, list) or not command
                or not all(isinstance(value, str) and value and '\0' not in value for value in command)
                or not PureWindowsPath(command[0]).is_absolute()):
            raise ValueError('Guest driver arm needs an absolute Windows command array: ' + name)
        env = arm.get('env', {})
        if not isinstance(env, dict) or not all(
                isinstance(key, str) and key and '=' not in key and '\0' not in key
                and isinstance(value, str) and '\0' not in value for key, value in env.items()):
            raise ValueError('Guest driver env must map names to strings: ' + name)
        if 'cwd' in arm and (not isinstance(arm['cwd'], str) or '\0' in arm['cwd']
                             or not PureWindowsPath(arm['cwd']).is_absolute()):
            raise ValueError('Guest driver cwd must be an absolute Windows path: ' + name)
    return config


def bootstrap_inputs(hcu_archive, cua_archive, guest_driver_config=None, bridge_script=None,
                     activation_worker=None, activation_revision=None):
    cua = resolve_cua_archive(cua_archive)
    config = load_guest_driver_config(guest_driver_config)
    hcu = Path(hcu_archive) if hcu_archive is not None else None
    bridge_script = Path(bridge_script) if bridge_script is not None else REPO / 'integration/guest_mcp_bridge.py'
    if hcu is None or not hcu.is_file():
        raise FileNotFoundError('HCU archive is missing: ' + str(hcu))
    if not bridge_script.is_file():
        raise FileNotFoundError('Bridge script is missing; select --bridge-script: ' + str(bridge_script))
    if bool(activation_worker) != bool(activation_revision):
        raise ValueError('Legacy worker deployment requires both source and expected revision')
    if activation_worker is not None and not Path(activation_worker).is_file():
        raise FileNotFoundError('Activation worker source is missing')
    return hcu, cua, config, bridge_script


def bootstrap(url, directory, hcu_archive=None, cua_archive=None, guest_driver_config=None,
              *, bridge_script=None, activation_worker=None, activation_revision=None,
              activation_server_dir=r'C:\OEM\server'):
    hcu, cua, config, bridge_script = bootstrap_inputs(
        hcu_archive, cua_archive, guest_driver_config, bridge_script, activation_worker, activation_revision)
    execute(url, "import pathlib; pathlib.Path(r'C:\\waa-bridge').mkdir(exist_ok=True)")
    uia_runtime = execute(url, r'''import comtypes,json,os,platform,sys
from pathlib import Path
dll=Path(os.environ['WINDIR'])/'System32'/'UIAutomationCore.dll'
assert dll.is_file(), 'Windows UIAutomationCore runtime is missing'
print(json.dumps({'python':sys.version,'comtypes':comtypes.__version__,
                  'windows':platform.version(),'uia_runtime':str(dll)}))''')
    (directory / 'uia-runtime.json').write_text(json.dumps(uia_runtime, indent=2))
    if activation_worker is not None:
        upload(url, Path(activation_worker), r'C:\waa-bridge\windows_window_activation.py')
        upload(url, Path(__file__).with_name('windows_activation_deployment.py'),
               r'C:\waa-bridge\windows_activation_deployment.py')
        activation_command = [r'C:\waa-bridge\windows_activation_deployment.py',
                              '--source', r'C:\waa-bridge\windows_window_activation.py',
                              '--server-dir', activation_server_dir, '--expected-revision', activation_revision]
        activation_deployment = deploy_activation_worker(url, activation_command, activation_revision, activation_server_dir)
        (directory / 'activation-worker.json').write_text(json.dumps(activation_deployment, indent=2))
    for source, target in [(hcu, r'C:\waa-bridge\hcu.zip'), (cua, r'C:\waa-bridge\cua.zip'),
                           (bridge_script, r'C:\waa-bridge\guest_mcp_bridge.py'),
                           (token_path(), r'C:\waa-bridge\bridge-token.txt')]:
        print('Uploading ' + source.name, file=sys.stderr, flush=True)
        upload(url, source, target)
    config_path = directory / 'arms.json'
    config_path.write_text(json.dumps(config))
    upload(url, config_path, r'C:\waa-bridge\arms.json')
    code = r'''
import os, pathlib, zipfile, shutil, subprocess, ctypes, json, sys
p=pathlib.Path(r'C:\waa-bridge')
''' + DRIVER_INSTALL + r'''
session=ctypes.c_ulong()
ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(),ctypes.byref(session))
if session.value==0: raise RuntimeError('WAA server child is in Session 0')
subprocess.run(['netsh','advfirewall','firewall','add','rule','name=WAA MCP controller','dir=in','action=allow','protocol=TCP','localport=8765'],check=True,capture_output=True)
out=open(p/'bridge.log','ab',buffering=0)
python_dir=pathlib.Path(sys.executable).parent
bridge_env=dict(os.environ)
bridge_env['PATH']=str(python_dir)+';'+str(python_dir/'Scripts')+';'+bridge_env.get('PATH','')
proc=subprocess.Popen([sys.executable,'-u',str(p/'guest_mcp_bridge.py'),'--config',str(p/'arms.json'),'--token-file',str(p/'bridge-token.txt'),'--host','0.0.0.0','--port','8765'],env=bridge_env,stdin=subprocess.DEVNULL,stdout=out,stderr=out,creationflags=subprocess.CREATE_NO_WINDOW)
print(json.dumps({'bridge_pid':proc.pid,'desktop_session':session.value,'username':os.environ.get('USERNAME')}))
'''
    result = execute(url, code)
    (directory / 'bootstrap.json').write_text(json.dumps(result, indent=2))
    return config


def save_binding(directory, binding):
    targets = [directory / 'episode.json']
    if os.environ.get('WAA_RESET_RECORD_FILE'):
        targets.append(Path(os.environ['WAA_RESET_RECORD_FILE']))
    for target in targets:
        temporary = target.with_suffix('.tmp')
        temporary.write_text(json.dumps(binding, indent=2) + '\n')
        temporary.replace(target)


def capture_container(name, directory):
    # Every cleanup attempt retains its own diagnostics. Failed recaptures after
    # removal must not overwrite the last usable pre-teardown evidence.
    capture = directory / 'container-captures' / f'{time.time_ns()}-{secrets.token_hex(4)}'
    capture.mkdir(parents=True, exist_ok=False)
    metadata = {'container': name, 'files': {}}
    for suffix, argv in [('container.log', ['docker', 'logs', '--tail', '500', name]),
                         ('container-inspect.json', ['docker', 'inspect', name])]:
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
            output = result.stdout + result.stderr
            metadata['files'][suffix] = {'returncode': result.returncode}
            succeeded = result.returncode == 0
        except Exception as exc:
            output = str(exc)
            metadata['files'][suffix] = {'error': f'{type(exc).__name__}: {exc}'}
            succeeded = False
        (capture / suffix).write_text(output)
        target = directory / suffix
        if succeeded or not target.exists():
            target.write_text(output)
    (capture / 'capture.json').write_text(json.dumps(metadata, indent=2) + '\n')


def cleanup_error_text(exc):
    summary = f'{type(exc).__name__}: {exc}'
    if isinstance(exc, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
        stderr = exc.stderr or ''
        if isinstance(stderr, bytes):
            stderr = stderr.decode('utf-8', errors='replace')
        # Inspect stdout contains configuration; retain only bounded, escaped stderr.
        summary += '; stderr=' + json.dumps(stderr[:2048], ensure_ascii=True)
        if len(stderr) > 2048:
            summary += ' [truncated]'
    for note in getattr(exc, '__notes__', ()):
        summary += '; ' + note
    return summary


def inspect_container(name):
    try:
        return json.loads(command(['docker', 'inspect', name, '--format', '{{json .}}']))
    except subprocess.CalledProcessError as exc:
        # Docker CLI versions vary the diagnostic's case. Container identity
        # remains exact: case folding applies only to the message prefix.
        missing = r'(?i:(?:error(?: response from daemon)?: )?no such (?:container|object): )' + re.escape(name)
        if exc.returncode == 1 and re.fullmatch(missing, (exc.stderr or '').strip()):
            return None
        raise


def stop_binding(binding):
    name = binding['container']
    container = inspect_container(name)
    if container is None:
        return {'status': 'already_absent', 'container': name}
    labels = container.get('Config', {}).get('Labels') or {}
    if labels.get('waa.owner') != 'waa-v2' or labels.get('waa.episode') != binding['episode_id']:
        raise RuntimeError('Container ownership does not match this episode')
    # Pin mutations to the inspected object; its name may be reused after removal.
    container_id = container['Id']
    if not isinstance(container_id, str) or not re.fullmatch(r'[0-9a-f]{64}', container_id):
        raise RuntimeError('Docker inspect did not return a full container ID')
    audio_error = None
    if binding.get('audio_capture', {}).get('started'):
        try:
            stop_audio_capture(binding, container_id)
        except Exception as exc:
            # Failed recording must remain visible, but cannot leak an owned VM.
            audio_error = cleanup_error_text(exc)
    try:
        command(['docker', 'stop', '--time', '120', container_id], timeout=150)
        command(['docker', 'rm', container_id])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        # A concurrent cleanup can remove the owned object between any two calls.
        # Daemon and permission failures cannot establish that removal succeeded.
        try:
            absent = inspect_container(container_id) is None
        except Exception as inspect_error:
            exc.add_note('Container absence check failed: ' + cleanup_error_text(inspect_error))
            raise exc
        if not absent:
            raise
        result = {'status': 'already_absent', 'container': name,
                  'command_error': cleanup_error_text(exc)}
    else:
        result = {'status': 'stopped', 'container': name, 'storage_preserved': binding['storage']}
    if audio_error is not None:
        result['audio_capture_error'] = audio_error
    return result


def audio_monitor(binding, commands, container_id=None):
    # A recycled IP/name must never redirect capture mutations to another VM.
    container_id = container_id or binding['container_id']
    if not re.fullmatch(r'[0-9a-f]{64}', container_id):
        raise RuntimeError('Audio capture requires the owned Docker object ID')
    script = Path(__file__).with_name('qmp_audio.py').read_text()
    return json.loads(command(['docker', 'exec', container_id, 'python3', '-c', script,
                               json.dumps(commands)], timeout=25))


def capture_index(info):
    match = re.fullmatch(r'\[(\d+)\]: Capturing audio\(44100,16,2\) to /waa-audio/playback\.wav: \d+ bytes\s*', info)
    if not match:
        raise RuntimeError('QEMU did not report exactly the owned playback capture: ' + repr(info))
    return int(match[1])


def start_audio_capture(binding):
    if audio_monitor(binding, ['info capture']) != ['']:
        raise RuntimeError('Unexpected existing QEMU audio capture')
    results = audio_monitor(binding, ['wavcapture /waa-audio/playback.wav waa-audio 44100 16 2', 'info capture'])
    if results[0] != '':
        raise RuntimeError('QEMU rejected playback capture: ' + repr(results[0]))
    capture_index(results[1])
    binding['audio_capture'].update(started=True, start_observation=results[1])


def stop_audio_capture(binding, container_id=None):
    index = capture_index(audio_monitor(binding, ['info capture'], container_id)[0])
    results = audio_monitor(binding, [f'stopcapture {index}', 'info capture'], container_id)
    if results != ['', '']:
        raise RuntimeError('QEMU did not confirm playback capture termination: ' + repr(results))
    binding['audio_capture'].update(started=False, monitor_stopped=True)


def capture_and_stop(binding, directory):
    """Keep evidence failures visible without allowing them to block owned VM cleanup."""
    capture_error = None
    try:
        capture_container(binding['container'], directory)
    except Exception as exc:
        capture_error = f'{type(exc).__name__}: {exc}'
    try:
        cleanup = stop_binding(binding)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(cleanup_error_text(exc)) from exc
    if capture_error is not None:
        cleanup['capture_error'] = capture_error
    binding['cleanup'] = cleanup
    if capture_error is not None:
        print('Container evidence capture failed: ' + capture_error, file=sys.stderr, flush=True)
    return cleanup


def verify_network_lock(name):
    # Each command runs in this container's network namespace, never on the host.
    script = r'''set -eu
test -s /run/waa-network-locked
dev=$(cat /run/waa-network-locked)
for firewall in iptables-legacy ip6tables-nft; do
    "$firewall" -C FORWARD -i dockerbridge -o "$dev" -j WAA_EGRESS
    "$firewall" -C WAA_EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
    "$firewall" -C WAA_EGRESS -j REJECT
    "$firewall" -C INPUT -i dockerbridge -p udp --dport 53 -j REJECT
    "$firewall" -C INPUT -i dockerbridge -p tcp --dport 53 -j REJECT
done
iptables-legacy --version
ip6tables-nft --version
for backend in iptables-legacy-save iptables-nft-save ip6tables-legacy-save ip6tables-nft-save; do
    echo "Backend: $backend"
    if "$backend"; then :; else echo "Backend unavailable: $backend (exit $?)"; fi
done
'''
    return command(['docker', 'exec', name, 'bash', '-c', script])


def verify_guest_clock(url, windows_timezone='UTC'):
    result = execute(url, "import datetime,json,subprocess; print(json.dumps({'timezone':subprocess.check_output(['tzutil','/g'],text=True).strip(),'utc':datetime.datetime.now(datetime.timezone.utc).isoformat()}))")
    clock = json.loads(result['output'])
    if clock['timezone'] != windows_timezone:
        raise RuntimeError('Guest timezone differs from QEMU localtime baseline: ' + clock['timezone'])
    guest_time = datetime.datetime.fromisoformat(clock['utc']).timestamp()
    clock['host_difference_seconds'] = round(guest_time - time.time(), 3)
    if abs(clock['host_difference_seconds']) > 120:
        raise RuntimeError('Guest UTC clock differs from host by more than 120 seconds: ' + str(clock))
    return clock


def verify_guest_license(url):
    script = "Get-CimInstance SoftwareLicensingProduct | Where-Object { $_.Name -like 'Windows*' -and $_.PartialProductKey } | Select-Object Name,LicenseStatus,GracePeriodRemaining,EvaluationEndDate | ConvertTo-Json -Compress"
    argv = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script]
    result = execute(url, f"import subprocess; print(subprocess.check_output({argv!r},text=True,timeout=30))")
    products = json.loads(result['output'])
    if not isinstance(products, list):
        products = [products]
    if not any(product and product.get('LicenseStatus') == 1 for product in products):
        raise RuntimeError('Windows reports no active license at the verified host UTC time')
    return products


def verify_qemu(name, timezone='UTC', capture_audio=False):
    code = r'''import json,pathlib
found=[]
for proc in pathlib.Path('/proc').iterdir():
    if not proc.name.isdigit(): continue
    try:
        args=proc.joinpath('cmdline').read_bytes().decode().rstrip('\0').split('\0')
        if pathlib.Path(args[0]).name!='qemu-system-x86_64': continue
        timezone=next((x[3:] for x in proc.joinpath('environ').read_bytes().decode().split('\0') if x.startswith('TZ=')),None)
        found.append({'argv':args,'timezone':timezone})
    except FileNotFoundError: pass
print(json.dumps(found))
'''
    processes = json.loads(command(['docker', 'exec', name, 'python3', '-c', code]))
    if len(processes) != 1:
        raise RuntimeError('Expected exactly one QEMU process')
    observed = processes[0]
    argv = observed['argv']
    if '-enable-kvm' not in argv and not any('accel=kvm' in arg for arg in argv):
        raise RuntimeError('QEMU did not enable KVM')
    if '-rtc' not in argv or argv[argv.index('-rtc') + 1] != 'base=localtime' or observed['timezone'] != timezone:
        raise RuntimeError('QEMU RTC or timezone differs from the selected Windows baseline')
    if capture_audio:
        for option, value in zip(AUDIO_CAPTURE_ARGS[::2], AUDIO_CAPTURE_ARGS[1::2]):
            if not any(left == option and right == value for left, right in zip(argv, argv[1:])):
                raise RuntimeError('QEMU did not enable the selected playback capture device/backend')
        observed['playback_capture_arguments_verified'] = True
    return observed


def container_command(args, name, storage, directory):
    qemu_arguments = '-qmp tcp:0.0.0.0:7200,server,nowait'
    capture_audio = getattr(args, 'capture_audio', False)
    if capture_audio:
        qemu_arguments += ' ' + ' '.join(AUDIO_CAPTURE_ARGS)
    run = ['docker', 'run', '-d', '--name', name, '--label', 'waa.owner=waa-v2',
           '--label', 'waa.episode=' + directory.name, '--network', args.network,
           '--device=/dev/kvm', '--device=/dev/net/tun', '--cap-add=NET_ADMIN',
           '--stop-timeout', '120', '--memory', '12g', '--cpus', '8',
           '-e', 'RAM_SIZE=8G', '-e', 'CPU_CORES=8',
           '-e', 'HOST_PORTS=7200', '-e', 'HV=' + args.hyperv, '-e', 'KVM=Y',
           '-e', 'TZ=' + getattr(args, 'timezone', 'UTC'),
           '-e', 'VERSION=win11x64-enterprise-eval', '-e', 'ARGUMENTS=' + qemu_arguments,
           '-v', str(storage) + ':/storage']
    if capture_audio:
        audio_directory = directory / 'audio'
        audio_directory.mkdir(mode=0o700)
        # QEMU retains an existing file's owner instead of creating root-owned
        # evidence that the operator cannot archive after container removal.
        descriptor = os.open(audio_directory / 'playback.wav', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        run += ['-v', str(audio_directory) + ':/waa-audio']
    if args.lock_network:
        hook = directory / 'network-lock.sh'
        hook.write_text(NETWORK_LOCK)
        run += ['-v', str(hook) + ':/waa-network-lock.sh:ro', '--entrypoint', '/bin/bash', args.image, '-c', LOCK_ENTRYPOINT]
    else:
        run += [args.image]
    return run


def start(args):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,90}', args.run_id):
        raise ValueError('Invalid run ID')
    episode = f'{args.run_id}-{args.arm}-{time.time_ns()}'
    directory = ROOT / 'vms' / episode
    directory.mkdir(parents=True, exist_ok=False)
    storage = directory / 'storage'
    storage.mkdir()
    snapshot = args.snapshot.resolve()
    hcu_archive = args.hcu_archive.resolve()
    cua_archive = getattr(args, 'cua_archive', None)
    guest_driver_config = getattr(args, 'guest_driver_config', None)
    if guest_driver_config is not None:
        guest_driver_config = Path(guest_driver_config).resolve()
    name = 'waa-' + episode.lower().replace('_', '-')
    binding = {'episode_id': episode, 'arm': args.arm, 'container': name,
               'snapshot': str(snapshot), 'hcu_archive': str(hcu_archive),
               'cua_archive': str(Path(cua_archive).resolve()) if cua_archive is not None else None,
               'guest_driver_config': str(guest_driver_config) if guest_driver_config is not None else None,
               'runtime_image': args.image, 'network': args.network, 'storage': str(storage), 'status': 'creating',
               'qemu_timezone': getattr(args, 'timezone', 'UTC'), 'qemu_rtc_base': 'localtime', 'kvm': True}
    binding['windows_timezone'] = getattr(args, 'windows_timezone', 'UTC')
    binding['bridge_script'] = str(getattr(args, 'bridge_script', None) or REPO / 'integration/guest_mcp_bridge.py')
    binding['activation_worker'] = str(args.activation_worker) if getattr(args, 'activation_worker', None) else None
    binding['activation_revision'] = getattr(args, 'activation_revision', None)
    binding['hyperv_enlightenments'] = args.hyperv
    if getattr(args, 'capture_audio', False):
        binding['audio_capture'] = {
            'requested': True, 'device': 'ich9-intel-hda', 'codec': 'hda-output', 'backend': 'none',
            'capture_method': 'qmp-hmp-wavcapture',
            'capture_file': str(directory / 'audio/playback.wav'),
            'sample_rate_hz': 44100, 'channels': 2, 'sample_format': 's16',
            'real_time_speaker': False, 'microphone': False,
        }
    binding['network_lock_requested'] = args.lock_network
    binding['initial_egress_blocked'] = False
    binding['cleanup_command'] = [sys.executable, str(Path(__file__).resolve()), 'stop', '--episode', str(directory / 'episode.json')]
    save_binding(directory, binding)
    attempted_run = False
    try:
        if (snapshot / 'data.img').stat().st_size <= 0:
            raise RuntimeError('Snapshot disk is empty')
        _, cua_archive, _, _ = bootstrap_inputs(
            hcu_archive, cua_archive, guest_driver_config, getattr(args, 'bridge_script', None),
            getattr(args, 'activation_worker', None), getattr(args, 'activation_revision', None))
        binding['cua_archive'] = str(cua_archive)
        # Reflink when available; a sparse copy also owns independent writable data on ext4.
        command(['cp', '-a', '--reflink=auto', '--sparse=always', str(snapshot) + '/.', str(storage)], timeout=900)
        run = container_command(args, name, storage, directory)
        attempted_run = True
        binding['container_id'] = command(run, timeout=180)
        ip = command(['docker', 'inspect', name, '--format', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'])
        binding.update(status='booting', vm_ip=ip, controller_url=f'http://{ip}:5000', bridge_url=f'http://{ip}:8765')
        save_binding(directory, binding)
        until = time.monotonic() + args.boot_timeout
        last = None
        while time.monotonic() < until:
            try:
                request(binding['controller_url'] + '/probe', timeout=3)
                break
            except Exception as exc:
                last = str(exc)
                running = command(['docker', 'inspect', name, '--format', '{{.State.Running}}'])
                if running != 'true':
                    raise RuntimeError('VM exited before its controller became ready')
                time.sleep(5)
        else:
            raise TimeoutError('Windows control server not ready: ' + str(last))
        if args.lock_network:
            (directory / 'network-rules.txt').write_text(verify_network_lock(name))
            binding['initial_egress_blocked'] = True
        binding['qemu_observed'] = verify_qemu(name, getattr(args, 'timezone', 'UTC'),
                                             getattr(args, 'capture_audio', False))
        if getattr(args, 'capture_audio', False):
            start_audio_capture(binding)
        binding['guest_clock'] = verify_guest_clock(binding['controller_url'], getattr(args, 'windows_timezone', 'UTC'))
        binding['guest_license'] = verify_guest_license(binding['controller_url'])
        print('Windows control server ready: ' + name, file=sys.stderr, flush=True)
        binding['status'] = 'bootstrapping'
        save_binding(directory, binding)
        bootstrap(binding['controller_url'], directory, hcu_archive, cua_archive, guest_driver_config,
                  bridge_script=getattr(args, 'bridge_script', None),
                  activation_worker=getattr(args, 'activation_worker', None),
                  activation_revision=getattr(args, 'activation_revision', None),
                  activation_server_dir=getattr(args, 'activation_server_dir', r'C:\OEM\server'))
        for _ in range(20):
            try:
                health = request(binding['bridge_url'] + '/health', {}, timeout=3,
                                 headers={'X-Bridge-Token': token_path().read_text().strip()})
                binding['bridge_health'] = health
                break
            except Exception:
                time.sleep(1)
        else:
            result = execute(binding['controller_url'], "from pathlib import Path; print(Path(r'C:\\waa-bridge\\bridge.log').read_text())")
            raise RuntimeError('Bridge failed to start: ' + str(result))
        if args.lock_network:
            (directory / 'network-rules-after-bootstrap.txt').write_text(verify_network_lock(name))
        binding['status'] = 'ready'
        save_binding(directory, binding)
        # A disconnected caller has not received ownership; treat failed delivery
        # like any other reset failure and reclaim this episode's VM.
        print(json.dumps(binding), flush=True)
    except BaseException as exc:
        binding.update(status='failed', failure=f'{type(exc).__name__}: {exc}')
        try:
            save_binding(directory, binding)
        except Exception as record_error:
            exc.add_note(f'Could not save failed VM binding: {record_error}')
        if attempted_run:
            try:
                capture_and_stop(binding, directory)
            except Exception as cleanup_error:
                binding['cleanup'] = {'status': 'error', 'error': str(cleanup_error)}
                exc.add_note(f'Owned VM cleanup failed: {cleanup_error}')
        try:
            save_binding(directory, binding)
        except Exception as record_error:
            exc.add_note(f'Could not save VM cleanup binding: {record_error}')
        try:
            (directory / 'failure.json').write_text(json.dumps(binding, indent=2) + '\n')
        except Exception as record_error:
            exc.add_note(f'Could not save VM failure report: {record_error}')
        raise


def main():
    global ROOT
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['reset', 'stop'])
    parser.add_argument('--arm', choices=['hcu', 'cua'], default='hcu')
    parser.add_argument('--root', type=Path, default=ROOT, help='Owner state directory; WAA_ROOT also sets the default')
    parser.add_argument('--run-id')
    parser.add_argument('--image', help='Explicit prepared Windows container image tag')
    parser.add_argument('--network', help='Existing Docker network for these guests')
    parser.add_argument('--timezone', default='UTC', help='Container IANA timezone matching the snapshot')
    parser.add_argument('--windows-timezone', default='UTC', help='Expected Windows timezone ID')
    parser.add_argument('--boot-timeout', type=int, default=600)
    parser.add_argument('--hyperv', choices=['Y', 'N'], default='N')
    parser.add_argument('--capture-audio', action='store_true',
                        help='Expose HDA playback and capture PCM to episode/audio/playback.wav; no live speaker or microphone')
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--hcu-archive', type=Path)
    parser.add_argument('--cua-archive', type=Path, help='Explicit upstream CUA Windows ZIP; installed without binary changes')
    parser.add_argument('--bridge-script', type=Path, help='guest_mcp_bridge.py from the MCP bridge integration')
    parser.add_argument('--activation-worker', type=Path, help='Opt-in legacy image worker replacement; otherwise use the installed controller')
    parser.add_argument('--activation-revision', help='Expected worker CHANGE_ID for legacy replacement')
    parser.add_argument('--activation-server-dir', default=r'C:\OEM\server')
    parser.add_argument('--guest-driver-config', type=Path, help='Developer JSON configuration for both guest driver commands and environments')
    parser.add_argument('--lock-network', action='store_true', help='Block guest-initiated egress while preparing the baseline')
    parser.add_argument('--episode')
    args = parser.parse_args()
    ROOT = args.root.resolve()
    if args.boot_timeout <= 0:
        parser.error('boot-timeout must be positive')
    if args.action == 'reset':
        for field in ('run_id', 'image', 'network', 'snapshot', 'hcu_archive', 'cua_archive'):
            if not getattr(args, field):
                parser.error('reset requires --' + field.replace('_', '-'))
        def terminated(signum, _frame):
            raise SystemExit(f'VM reset interrupted by signal {signum}')
        previous = signal.signal(signal.SIGTERM, terminated)
        try:
            start(args)
        finally:
            signal.signal(signal.SIGTERM, previous)
    else:
        if not args.episode:
            parser.error('stop requires --episode')
        binding = json.loads(Path(args.episode).read_text())
        directory = Path(args.episode).parent
        cleanup = capture_and_stop(binding, directory)
        save_binding(directory, binding)
        print(json.dumps(cleanup))


if __name__ == '__main__':
    main()
