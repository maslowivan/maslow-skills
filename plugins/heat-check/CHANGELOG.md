# Changelog — heat-check

Versions follow [semantic versioning](https://semver.org). Every change to the
plugin bumps `version` in both `.claude-plugin/plugin.json` and
`.codex-plugin/plugin.json` and adds a section here; CI enforces it and tags the
release as `heat-check-v<version>`.

## 0.1.0 — 2026-10-09

- **Added:** the `heat-check` skill and `scripts/heat_check.py`: a read-only
  snapshot of CPU per process and per app (2 s sample), load, memory pressure,
  swap and compressor, thermal warnings and CPU speed limit, power source and
  Low Power Mode, display scaling (macOS) and temperatures (Linux), turned into
  findings with evidence and advice (`WINDOWSERVER_BUSY`, `KERNEL_TASK_COOLING`,
  `HOT_PROCESS`, `BUSY_APP`, `HEAVY_SWAP`, `MEMORY_PRESSURE`, `SCREEN_CAPTURE`,
  `SCALED_DISPLAY`, `SPOTLIGHT_INDEXING`, ...). Text report or `--json`.
