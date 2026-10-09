# heat-check

Ask your agent "why is my Mac so hot?" and get an answer grounded in numbers:
which processes and apps burn the CPU, whether macOS is already throttling,
whether memory is short and swapping, and whether the display setup or a screen
capture keeps `WindowServer` busy, plus what to do about each one.

Works in **Codex** and **Claude Code**. Read-only: it never kills a process or
changes a setting; the agent asks before doing anything.

## What it checks

| Area | macOS | Linux |
| --- | --- | --- |
| CPU per process over a 2 s sample, summed per app (Electron helpers grouped) | `top`, `ps` | `/proc` |
| Load average vs. cores, user/sys/idle split | ✓ | ✓ |
| Memory pressure, swap, compressor | `sysctl`, `top` | `/proc/meminfo` |
| Thermal warnings, CPU speed limit | `pmset -g therm` | cpufreq limits |
| Temperatures | needs root, see below | `/sys/class/thermal` |
| Power source, battery, Low Power Mode | `pmset` | `/sys/class/power_supply` |
| Display scaling (e.g. a 4K panel rendering 5120x2880) | `system_profiler` | – |

The findings explain the usual traps: `kernel_task` is macOS cooling down (a
symptom, not a cause), `WindowServer` load comes from scaled resolutions, large
displays and screen capture, full swap costs CPU even when pressure looks
"normal", and Spotlight/Photos/Time Machine jobs are temporary.

## Use

Install the plugin (more in the [repository README](../../README.md#install)):

```sh
# Codex
codex plugin marketplace add maslowivan/maslow-skills
codex plugin add heat-check@maslow-skills

# Claude Code
claude plugin marketplace add maslowivan/maslow-skills
claude plugin install heat-check@maslow-skills
```

Then ask in your own words: "my laptop is overheating", "check the CPU load", "why are the
fans loud". The script can also be run directly:

```sh
python3 skills/heat-check/scripts/heat_check.py          # report
python3 skills/heat-check/scripts/heat_check.py --json   # for agents and scripts
```

Options: `--interval N` (sample seconds, default 2), `--top N` (processes,
default 15), `--skip displays` (skip the slowest collector).

Example (abridged):

```
Load: 2.93 2.62 3.13
CPU: user 10.73%  sys 6.46%  idle 82.79%
Memory: total 24.0G, used 23.0G, compressor 6.6G, free 296.0M, swap 10.3G/11.0G
Display External 4K: renders 5120x2880, looks like 2560x1440 (scaled)

Findings:
  [medium] The window compositor is the biggest CPU consumer
      evidence: WindowServer 50% CPU; scaled displays: ... renders 5120x2880 for a 3840x2160 panel; screen capture active: ...
  [medium] kernel_task is holding the CPU back (heat or charger symptom)
  [medium] Swap is nearly full
```

## Temperatures on macOS

macOS does not expose sensor temperatures to unprivileged processes, so the
skill reports throttling state instead. For real numbers run yourself:

```sh
sudo powermetrics --samplers smc,thermal -i 2000 -n 1
```

## Requirements

Python 3.9+ (the macOS system `/usr/bin/python3` is enough), standard library
only. macOS 12+ or Linux.
