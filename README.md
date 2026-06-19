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
- Mock status, teleop, recording, and local dataset inspect/validate commands.
- A lightweight LeRobot v3-compatible local writer using Parquet plus MP4.
- Per-episode and dataset-level state/action stats for locally written datasets.

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
- A browser status page for dual-arm bring-up.

## Feature Matrix

| Workflow | Command | Owning modules | Status |
| --- | --- | --- | --- |
| Serial discovery | `detect ports` | `hardware.ports` | Implemented |
| SOARM bus probe | `detect ports --probe-soarm` | `hardware.ports` | Implemented, requires `soarm-sdk` |
| USB camera discovery | `detect cameras` | `hardware.cameras` | Implemented, macOS USB metadata first |
| Camera preview | `preview cameras` | `hardware.cameras` | Implemented, requires OpenCV |
| Role assignment | `assign arms`, `assign cameras` | `assignment`, `config` | Implemented |
| Status check | `status` | `hardware.arms`, `hardware.cameras`, `cli` | Mock path implemented; hardware path depends on local configs |
| Teleoperation | `teleop` | `teleop.loop`, `hardware.arms` | Mock path tested; hardware path needs validation |
| Recording | `record` | `recording.session`, `teleop.loop`, `datasets.lerobot_v3` | Mock path tested; camera/video path requires OpenCV |
| Dataset inspection | `dataset inspect`, `dataset validate` | `datasets.tools`, `datasets.lerobot_v3` | Implemented for current local layout |

## Module Boundaries

- `soarm_studio.hardware.ports`: serial discovery and SOARM servo-bus probing.
- `soarm_studio.hardware.cameras`: USB camera discovery and OpenCV preview or
  capture. It does not do software color correction by default.
- `soarm_studio.hardware.arms`: thin adapter from session config to
  `soarm-sdk`.
- `soarm_studio.assignment`: writes local role mappings for arms and cameras.
- `soarm_studio.teleop`: direct leader-to-follower loop with clipping and
  metrics.
- `soarm_studio.recording`: connects a session config to the dataset writer.
- `soarm_studio.datasets`: local LeRobot v3-compatible schema, writer, inspect,
  and validate helpers.
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
soarm-studio teleop --config configs/sessions/mock.yaml --seconds 2
soarm-studio record --config configs/sessions/mock.yaml --seconds 2 --task "mock pick" --overwrite
soarm-studio dataset inspect datasets/mock-dual-soarm
soarm-studio dataset validate datasets/mock-dual-soarm
```

The config files are JSON-compatible YAML, so they still load without `PyYAML`.

## Pipeline Step 1: Bring-Up

When the arms and cameras are connected through a dock, do bring-up in separate
passes. Arms appear as USB serial devices. USB cameras usually do not appear
under `/dev/cu.*`.

First identify, probe, and assign the arms:

```bash
soarm-studio detect ports --include-system
soarm-studio detect ports \
  --probe-soarm \
  --arm-config ../soarm/configs/soarm.yaml
soarm-studio assign arms \
  --leader-port /dev/cu.usbmodemLEADER \
  --follower-port /dev/cu.usbmodemFOLLOWER \
  --base-arm-config ../soarm/configs/soarm.yaml
```

`assign arms` writes local hardware files by default:

- `configs/sessions/local.yaml`
- `configs/arms/leader.yaml`
- `configs/arms/follower.yaml`

Probe one arm bus at a time. On macOS, use `/dev/cu.*` ports for outbound
serial connections from SOARM. The matching `/dev/tty.*` device is often the
dial-in side of the same USB serial interface and should not be saved in the arm
config.

Then identify, preview, and assign the cameras:

```bash
soarm-studio detect cameras
soarm-studio preview cameras --indices 0,1,2,3 --backend avfoundation --output-dir previews/cameras
soarm-studio assign cameras \
  --backend avfoundation \
  --wrist-index 0 \
  --third-person-index 1
```

Camera detection reads USB metadata by default and does not import OpenCV.
Preview is the explicit OpenCV step. Inspect the saved preview images before
assigning `wrist` and `third_person`.

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
