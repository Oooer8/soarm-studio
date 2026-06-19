# SOARM Studio

SOARM Studio is a thin application layer above `soarm-sdk` for dual-arm
bring-up, teleoperation, recording, local LeRobot v3 style datasets, and later
deployment clients.

It is intentionally not a LeRobot clone. The runtime path stays direct:

```text
leader arm -> teleop loop -> safety clipping -> follower arm
                         -> recorder -> LeRobot v3-compatible files
```

## Current Status

Implemented:

- Serial port detection with macOS `/dev/cu.*` preference and USB metadata.
- SOARM bus probing through `soarm-sdk` (`SOARMConfig` and `ServoBus`).
- USB camera detection through macOS USB metadata without importing OpenCV.
- Camera preview through explicit OpenCV backends (`auto`, `avfoundation`,
  `default`, `any`).
- Leader/follower and wrist/third-person assignment into a local session config.
- Runtime-owned mock status, preflight, teleop, recording, and local dataset
  inspect/validate commands.
- A lightweight LeRobot v3-compatible local writer using Parquet plus MP4.
- Per-episode and dataset-level state/action stats for locally written datasets.
- Control-sample recording semantics where `observation.state` is the follower
  state before the action is sent.
- Recording sidecars for hardware/session snapshots and sync quality.
- A local Web UI/API skeleton for the five-step bring-up and recording workflow.

Partially implemented:

- Real dual-arm teleop is wired through `soarm-sdk`, but still needs hardware
  validation for your two-arm setup.
- Multi-camera recording is wired through OpenCV, but camera index stability
  should be confirmed with preview before recording.
- Dataset compatibility targets LeRobot v3 layout, but broad edit operations
  are not implemented yet.
- The local dataset writer creates a fresh dataset root. Appending to an
  existing dataset is intentionally out of scope for now.

Not implemented yet:

- Dataset replay, delete episodes, split, merge, feature rename/remove, stats
  refresh, image-to-video conversion, and Hugging Face Hub push/pull.
- Policy runner and async policy server/client.
- Full production React build integration and hardware-validated Web
  calibration controls.

## Feature Matrix

| Workflow | Command | Owning modules | Status |
| --- | --- | --- | --- |
| Serial discovery | `detect ports` | `hardware.ports` | Implemented |
| SOARM bus probe | `detect ports --probe-soarm` | `hardware.ports` | Implemented, requires `soarm-sdk` |
| USB camera discovery | `detect cameras` | `hardware.cameras` | Implemented, macOS USB metadata first |
| Camera preview | `preview cameras` | `hardware.cameras` | Implemented, requires OpenCV |
| Role assignment | `assign arms`, `assign cameras` | `assignment`, `config` | Implemented |
| Runtime bindings | `bind ...`, `verify bindings` | `assignment`, `hardware.bindings` | Implemented |
| Status/preflight | `status`, `preflight` | `hardware.runtime`, `hardware.preflight` | Mock path tested; hardware path depends on local configs |
| Calibration | `calibrate --role ...` | `hardware.calibration`, `soarm-sdk` | Mock path tested; hardware path delegates to SDK |
| Teleoperation | `teleop --free-test` | `teleop.loop`, `hardware.runtime` | Mock path tested; hardware path needs validation |
| Recording | `record` | `recording.session`, `teleop.loop`, `datasets.lerobot_v3` | Mock path tested; camera/video path requires OpenCV |
| Dataset inspection | `dataset inspect`, `dataset validate`, `dataset rerun` | `datasets.tools`, `datasets.lerobot_v3` | Inspect/validate implemented; rerun requires `rerun-sdk` |
| Local Web | `web` | `web`, `webapp` | Mock API smoke-tested |

## Module Boundaries

- `soarm_studio.hardware.ports`: serial discovery and SOARM servo-bus probing.
- `soarm_studio.hardware.cameras`: USB camera discovery and OpenCV preview or
  capture. It does not do software color correction by default.
- `soarm_studio.hardware.arms`: thin adapter from session config to
  `soarm-sdk`.
- `soarm_studio.assignment`: writes local role mappings for arms and cameras.
- `soarm_studio.hardware.bindings`: builds and verifies saved hardware binding
  snapshots.
- `soarm_studio.hardware.preflight`: static and runtime readiness checks.
- `soarm_studio.hardware.runtime`: owns connected arms/cameras for a workflow
  and prevents in-process serial conflicts.
- `soarm_studio.teleop`: direct leader-to-follower loop with clipping and
  per-tick `ControlSample` metrics.
- `soarm_studio.recording`: connects a session config to the dataset writer.
- `soarm_studio.datasets`: local LeRobot v3-compatible schema, writer, inspect,
  and validate helpers.
- `soarm_studio.web`: local HTTP API and static Web UI for the five-step
  workflow.
- `soarm_studio.cli`: command routing only; hardware and dataset behavior should
  live in the modules above.

Generated local outputs such as `datasets/`, `previews/`, `configs/arms/*.yaml`,
and `configs/sessions/local*.yaml` are ignored by git. They are machine and
hardware specific.

## Development Notes

- Keep hardware dependencies lazy. Import `soarm-sdk`, OpenCV, NumPy, pyserial,
  and pyarrow inside the adapter/helper that needs them so the base package stays
  light.
- Keep the CLI thin. New behavior should land in `hardware`, `assignment`,
  `teleop`, `recording`, or `datasets`, with CLI handlers only parsing arguments
  and formatting JSON output.
- Treat `datasets/`, `previews/`, cache folders, and `*.egg-info` as generated
  artifacts. They are useful for local bring-up but should not contain durable
  source changes.
- The teleop loop is the latency-sensitive path. Avoid per-frame heavyweight
  imports or unnecessary hardware reads there; put slower video encoding and
  dataset metadata work at episode save/finalize boundaries.

## Quick Start

Create the conda environment:

```bash
conda env create -f environment.yml
conda activate soarm-studio
```

Run the mock path first:

```bash
soarm-studio status --config configs/sessions/mock.yaml
soarm-studio preflight --config configs/sessions/mock.yaml --overwrite
soarm-studio teleop --config configs/sessions/mock.yaml --seconds 2
soarm-studio record --config configs/sessions/mock.yaml --seconds 2 --task "mock pick" --overwrite
soarm-studio dataset inspect datasets/mock-dual-soarm
soarm-studio dataset validate datasets/mock-dual-soarm
soarm-studio web --config configs/sessions/mock.yaml --port 8000
```

The config files are JSON-compatible YAML, so they still load without `PyYAML`.

## Pipeline Step 1: Hardware Bring-Up

Step 1 discovers the local hardware, assigns stable roles, and writes the local
binding files. It stops before calibration, motion, teleoperation, or recording.
When the arms and cameras are connected through a dock, keep the arm pass and
camera pass separate. Arms appear as USB serial devices. USB cameras usually do
not appear under `/dev/cu.*`.

Step 1 is complete when:

- `configs/sessions/local.yaml` contains `leader`, `follower`, `wrist`, and
  `third_person` roles.
- `configs/arms/leader.yaml` and `configs/arms/follower.yaml` were generated
  from the same base SOARM arm config.
- The wrist and third-person camera indexes were chosen from saved preview
  images, not from USB metadata alone.
- `soarm-studio verify bindings --config configs/sessions/local.yaml` reports
  that the saved bindings still match the currently detected hardware.

### 1. List Candidate Arm Ports

Start by listing all visible serial ports:

```bash
soarm-studio detect ports --include-system
```

On macOS, save `/dev/cu.*` ports for SOARM. The matching `/dev/tty.*` device is
usually the dial-in side of the same USB serial interface and should not be
stored in the arm config.

### 2. Probe The SOARM Buses

Probe one arm bus at a time so it is clear which physical arm is attached to
which serial port:

```bash
soarm-studio detect ports \
  --probe-soarm \
  --arm-config ../soarm/configs/soarm.yaml
```

Use the probe output to decide which port is the leader arm and which port is
the follower arm. If two ports look identical, unplug one arm, probe again, then
label the cable or hub slot before moving on.

### 3. Assign Arm Roles

Write the local session config and per-arm config files:

```bash
soarm-studio assign arms \
  --leader-port /dev/cu.usbmodemLEADER \
  --follower-port /dev/cu.usbmodemFOLLOWER \
  --base-arm-config ../soarm/configs/soarm.yaml
```

`assign arms` writes:

- `configs/sessions/local.yaml`
- `configs/arms/leader.yaml`
- `configs/arms/follower.yaml`

`bind arms` is an alias for the same role-assignment workflow:

```bash
soarm-studio bind arms \
  --leader-port /dev/cu.usbmodemLEADER \
  --follower-port /dev/cu.usbmodemFOLLOWER \
  --base-arm-config ../soarm/configs/soarm.yaml
```

### 4. Detect Camera Candidates

Camera detection reads USB metadata by default and does not import OpenCV:

```bash
soarm-studio detect cameras
```

Treat this as inventory only. USB metadata may identify a device, but it does
not prove which OpenCV index is the wrist view.

### 5. Preview Camera Indexes

Capture preview frames through the backend that will be used for recording:

```bash
soarm-studio preview cameras \
  --indices 0,1,2,3 \
  --backend avfoundation \
  --output-dir previews/cameras
```

Open the saved images in `previews/cameras/` and identify the actual role for
each index. Preview is the source of truth for multi-camera mapping because
OpenCV indexes can change after unplugging devices, changing hubs, or rebooting.

### 6. Assign Camera Roles

Save the confirmed preview indexes:

```bash
soarm-studio assign cameras \
  --backend avfoundation \
  --wrist-index 1 \
  --third-person-index 0
```

The indexes above match a tested local setup where preview showed index `1` as
the wrist camera and index `0` as the third-person camera. Reuse different
indexes when your preview images show a different layout.

`bind cameras` is an alias for the same role-assignment workflow.

### 7. Verify Saved Bindings

After assigning arms and cameras, compare the saved session config against the
currently detected hardware:

```bash
soarm-studio verify bindings --config configs/sessions/local.yaml
```

If verification fails, repeat only the affected pass. For example, if the arm
ports still match but the camera roles are wrong, rerun `preview cameras` and
`assign cameras` instead of rewriting the arm configs.

After Step 1, use preflight as the gate before calibration, motion, or
recording:

```bash
soarm-studio preflight --config configs/sessions/local.yaml --overwrite
```

Then continue with the later workflow stages:

```bash
soarm-studio calibrate --config configs/sessions/local.yaml --role both
soarm-studio teleop --config configs/sessions/local.yaml --free-test --seconds 5
soarm-studio record \
  --config configs/sessions/local.yaml \
  --episodes 1 \
  --warmup 1 \
  --seconds 10 \
  --task "pick object" \
  --overwrite
```

Recording writes LeRobot v3 data plus local sidecars:

- `meta/soarm_session.json`
- `meta/sync_quality.json`
- `episodes/episode_000000/quality.json`

Camera image correction is not enabled in software by default. Prefer stable
lighting, camera placement, and consistent camera hardware for datasets so that
training and deployment see the same raw observation distribution.

## Dependency Strategy

The base package has no required runtime dependencies. Install heavier pieces
only when needed:

```bash
python -m pip install -e ".[ports]"
python -m pip install -e ".[dataset]"
python -m pip install -e ".[hardware]"
python -m pip install -e ".[lerobot-compat]"
```

In the provided conda environment, the common development extras and local
`../soarm` SDK checkout are installed editable.
