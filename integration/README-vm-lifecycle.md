# Operator-owned Windows VM lifecycle

`vm.py` creates a writable snapshot copy, starts one owned Windows container,
and installs the selected HCU and CUA driver packages. `remote_vm.py` runs
the same commands on a Linux VM host through an episode-specific SSH connection.
Each command prints a JSON binding with its endpoints and cleanup command.

These optional commands do not replace the upstream benchmark runner. They do
not contain task instructions, model endpoints, evaluator changes, or application
setup recipes. Application settings and installed software belong to external
baseline preparation. This extraction therefore does not reproduce every setting
of an existing deployment.

## Requirements

- Python 3.11 or newer on the operator and VM hosts. Remote cancellation requires
  Linux `pidfd_open` and `pidfd_send_signal` support.
- Docker access, GNU `cp`, `/dev/kvm`, and `/dev/net/tun` on the VM host.
- An existing Docker network and an explicitly selected Windows container image.
  The image must support the upstream `/storage` snapshot and QEMU environment
  conventions. These commands do not download or build images.
- A snapshot directory containing a nonempty `data.img` and its supporting VM
  files. Its Windows controller must already serve port 5000 in an interactive
  desktop session. Windows must have an active license and the selected timezone.
- Existing HCU and official CUA Windows ZIP packages. The installer replaces
  only their directories in the fresh guest. It does not patch driver binaries.
- `guest_mcp_bridge.py` from the separate MCP bridge integration. The bridge CLI
  must accept `--config`, `--token-file`, `--host`, and `--port`.
  Select it with `--bridge-script` if it is outside this checkout.
- The guest controller's Python environment must provide `comtypes`, and Windows
  must provide `UIAutomationCore.dll`. The bridge and its descendants use that
  interpreter. Its directory and `Scripts` directory precede the existing PATH
  only in the child environment; bootstrap preserves the registry PATH.

The controller and bridge endpoints belong on a trusted, isolated network.
The bridge uses a generated token stored with mode `0600` under the owner state
directory. Concurrent creators publish one complete token without replacing it.
The optional guest configuration selects commands and environment
variables; it does not install additional extensions or grant their permissions.

## Local VM host

Replace the example paths and image tag with prepared artifacts. The image's IANA
timezone and the snapshot's Windows timezone ID must describe the same timezone.
Both default to UTC. The command checks the guest clock without changing it.

```sh
python3 integration/vm.py reset \
  --root /srv/waa-state \
  --run-id comparison-one \
  --arm hcu \
  --image prepared-windows:1 \
  --network waa-evaluation \
  --snapshot /srv/waa-snapshots/windows \
  --hcu-archive /srv/waa-artifacts/hcu-windows.zip \
  --cua-archive /srv/waa-artifacts/cua-windows.zip \
  --bridge-script /srv/waa-bridge/guest_mcp_bridge.py
```

`--root` defaults to `WAA_ROOT`, or `~/.local/share/waa-v2` when unset. The
binding records the selected image tag, paths, configuration, and episode ID.
It does not use content hashes for admission.

The printed `cleanup_command` stops and removes only the container belonging to
that binding. For manual cleanup, pass its `episode.json` to the same script:

```sh
python3 integration/vm.py stop --episode /srv/waa-state/vms/EPISODE/episode.json
```

The writable storage copy and operator evidence remain after cleanup. Archive
them separately according to the operator's retention policy.

## Remote VM host

Configure SSH authentication before launching. Paths supplied as `--remote-root`,
`--remote-repo`, snapshot, archives, and bridge script refer to the VM host.
The repository on that host must contain these operator modules.

```sh
python3 integration/remote_vm.py reset \
  --root /srv/waa-controller-state \
  --ssh-host vm-host.example \
  --remote-root /srv/waa-state \
  --remote-repo /srv/WindowsAgentArena-V2 \
  --loopback-ip 127.0.0.2 \
  --run-id comparison-one \
  --arm cua \
  --image prepared-windows:1 \
  --network waa-evaluation \
  --snapshot /srv/waa-snapshots/windows \
  --hcu-archive /srv/waa-artifacts/hcu-windows.zip \
  --cua-archive /srv/waa-artifacts/cua-windows.zip \
  --bridge-script /srv/waa-bridge/guest_mcp_bridge.py
```

Each concurrent episode needs its own IPv4 loopback address. A private SSH control
socket forwards ports 5000, 8765, 9222, and 8080. The binding's
`cleanup_command` cancels its remote worker, removes its owned VM, and closes only
that SSH connection. It does not use an SSH profile's shared control socket.

Cancellation records a marker before signalling the worker. A pidfd and process
start time prevent signalling a reused PID. Cancellation before startup is also
recorded, so a delayed worker cannot start a VM after cancellation.

Container removal checks ownership labels and pins the inspected Docker object
ID. Repeated cleanup accepts only an explicit absence response for that object.
Missing Docker, daemon failures, permission errors, and unrelated object names
remain errors. A failed evidence write or disconnected caller still triggers
cleanup after the owner attempted container creation.

## Optional legacy activation worker replacement

Prefer a baseline whose controller already includes the supported activation
implementation. By default bootstrap does not modify the controller or its worker.

For a legacy image that already registers `windows_window_activation.install`,
explicitly supply `--activation-worker` and `--activation-revision`. Use
`--activation-server-dir` when the guest server is outside `C:\OEM\server`.
The compatibility script requires the existing single registration marker,
rejects an active worker, and atomically replaces only the worker file.
It stages the replacement beside the target to support a server on another volume.
It does not inject or rewrite `main.py`.

The script enumerates Python processes through Windows CIM, so it does not
require `psutil`. Bootstrap confirms the selected worker revision and, when
Flask's reloader is active, a new controller PID. A lost deployment response
triggers bounded confirmation, not a repeated deployment mutation.

## Optional guest egress isolation

`--lock-network` installs the firewall boundary inside the container before QEMU
starts. It requires NAT mode and the image's `iptables-legacy` and `ip6tables-nft`
backends. The script verifies those rules before and after bootstrap. It does
not change the host firewall. Keep this flag unset when the task requires
guest-initiated network access.

## Optional playback capture

`--capture-audio` adds an Intel HDA controller and an output-only codec to the
guest. A QEMU monitor capture writes playback samples to `audio/playback.wav` under that episode's
directory. The option is available in both local and remote reset commands;
without it, the commands add no audio device, backend, or capture mount.

This provides a guest playback endpoint and file capture. It does not provide
a real-time speaker or a microphone. The capture records 44.1 kHz, stereo,
16-bit signed PCM. A running VM can keep writing samples even during silence,
at approximately 176,400 bytes per second, so retain the existing episode time
limit and account for capture disk space.

Each episode mounts its own `audio` directory at `/waa-audio`, separately from
`storage`. The operator pre-creates the capture file with mode `0600` so it remains
readable by that operator after QEMU exits. The episode binding records the requested device, backend, format,
and capture path. Bootstrap verifies that the selected QEMU arguments are active;
this alone does not establish that Windows bound a driver or played nonzero audio.

The image must provide QEMU's `none`, `ich9-intel-hda`, `hda-output`, and monitor
`wavcapture` support. A capture callback records the mixed output independently
of hardware voice initialization. The `wav` backend can leave an empty file on
Windows even when a monitor capture receives the played samples.

The operator starts capture through QMP after the controller becomes ready.
Cleanup stops that capture before stopping the VM, which finalizes the WAV header.
Capture errors remain in cleanup metadata and do not prevent VM removal. A VM
that exits before cleanup may leave an unfinished recording. Check guest devices
and audio services when validating a new image. Use the returned cleanup command
before inspecting the final WAV header and PCM samples. Archive the
episode's `audio` directory with its other evidence; do not treat an empty or
all-zero WAV as proof of successful playback.

## Validation

Run the operator tests from the repository root:

```sh
python3 -m unittest discover -s integration -p 'test_*.py' -v
```

Tests use fake guest/controller/Docker responses, synthetic ZIPs, real local
socket binding, a cancellable subprocess, and a sparse file copy. They do not
start Windows, Docker containers, or SSH connections. Native Windows behavior
and a prepared image's boot conventions still require separate deployment
validation; passing these tests is not a benchmark result.
