<p align="center">
  <img src="docs/assets/brand/soarm-studio-logo-wordmark.png" alt="SOARM Studio" width="720">
</p>

# SOARM Studio

SOARM Studio exists beside LeRobot for the parts of SOARM operation where a
robot-specific workflow matters most: hardware bring-up, port assignment,
readiness checks, calibration, low-latency leader-to-follower teleoperation,
and local recording quality. LeRobot remains the target ecosystem for dataset
compatibility, training, policy deployment, and sharing.

The runtime path stays direct:

```text
leader arm -> teleop loop -> SDK direct joint stream -> follower arm
                         -> synchronized samples -> LeRobot-v3-compatible files
```

Use Studio to make the arm trustworthy on the table; use LeRobot to carry the
resulting data and policies into the broader robot-learning stack.

## Documentation

The README is intentionally short. Detailed installation, architecture,
hardware setup, calibration, recording, and troubleshooting live in the docs
site:

- [SOARM Studio Docs](https://oooer8.github.io/soarm-studio/index.html)

For local development, the static docs site starts at `docs/index.html`, with
each documentation section published as its own HTML page.

The docs site is designed for GitHub Pages and can later host images, videos,
code blocks, diagrams, and hardware walkthrough media under `docs/assets/`.

## Install

```bash
conda env create -f environment.yml
conda activate soarm-studio
```

The provided environment installs SOARM Studio in editable mode and pulls
`soarm-sdk` from the GitHub dependency declared by the `hardware` extra.

## Recommended Flow

```bash
soarm-studio scan --include-system
soarm-studio scan --probe-arms
soarm-studio setup arms \
  --leader-port /dev/cu.usbmodemLEADER \
  --follower-port /dev/cu.usbmodemFOLLOWER
soarm-studio scan --preview-cameras \
  --camera-indices 0,1,2,3 \
  --backend avfoundation \
  --output-dir previews/cameras
soarm-studio setup cameras \
  --backend avfoundation \
  --wrist-index 1 \
  --third-person-index 0
soarm-studio check --config configs/session.yaml --overwrite
soarm-studio calibrate --config configs/session.yaml --role leader
soarm-studio calibrate --config configs/session.yaml --role follower
soarm-studio check --config configs/session.yaml --overwrite
soarm-studio teleop --config configs/session.yaml --free-test --seconds 5
soarm-studio record --config configs/session.yaml --episodes 1 --warmup 1 --seconds 10 --task "pick object" --overwrite
```

Generated machine-local files are ignored by git:

- `configs/session.yaml`
- `configs/arms/*.yaml`
- `previews/`
- `datasets/`

## Mock Smoke Test

```bash
soarm-studio check --config configs/sessions/mock.yaml --overwrite
soarm-studio teleop --config configs/sessions/mock.yaml --seconds 2
soarm-studio record --config configs/sessions/mock.yaml --seconds 2 --task "mock pick" --overwrite
soarm-studio dataset inspect datasets/mock-soarm
soarm-studio dataset validate datasets/mock-soarm
```

## Development

```bash
conda run -n soarm-studio python -m pytest
conda run -n soarm-studio python -m ruff check .
```

Keep SOARM Studio as the application layer. Low-level arm behavior, motion,
safety, and calibration remain delegated to `soarm-sdk`.
