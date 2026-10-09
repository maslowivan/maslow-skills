---
name: heat-check
description: Find out why a computer is hot, loud or slow right now - per-process and per-app CPU over a short sample, load, memory pressure and swap, thermal throttling, power source and (on macOS) display scaling and screen capture - and explain the causes with concrete, safe fixes. Use when the user says their Mac/laptop is overheating, the fans are loud, it is sluggish, the battery drains fast, or asks to check CPU load, top processes or what is eating the CPU. Read-only; never kills processes or changes settings by itself.
---

# Heat check

A read-only diagnosis of what is heating or loading the machine. The bundled
script collects the facts and pre-computes findings; you explain them and
suggest fixes. macOS is the main target, Linux is supported.

## Run

```sh
python3 <this skill folder>/scripts/heat_check.py --json
```

- Takes about 3-5 s (a 2 s CPU sample plus `system_profiler` for displays).
  `--interval N` changes the sample, `--top N` the number of processes,
  `--skip displays` drops the slowest collector.
- Needs no root and no extra packages (Python 3.9+, macOS system Python works).
- Output: `snapshot` (load, CPU split, memory/swap/pressure, thermal, power,
  displays, top processes with their app), `apps` (CPU and memory summed per app)
  and `findings` (`severity` high/medium/info, `code`, `title`, `evidence`,
  `advice`), most severe first. On failure: `{"ok": false, "error": {...}}`.
- A heat spike can be over by the time you look. If `NOTHING_HOT` comes back but
  the user says it is hot now, run it again, or with `--interval 5`.

## Explain

Answer in the user's language, result first, short:

1. **Verdict** - is the CPU actually busy (idle %, load vs. cores) and what is
   the main source of heat.
2. **Causes** - the high/medium findings in order, each with its number
   (process and %CPU, swap used, display mode) and what that process is.
   Name the owning app (`app` field): `Claude Helper` belongs to Claude,
   a `SkyComputerUseService` inside "Codex Computer Use" belongs to Codex.
3. **What to do** - concrete actions from `advice`, most effective first.
4. Mention what you could not see (temperatures on macOS need root, see below).

Interpretation rules that are easy to get wrong:

- **kernel_task** high = macOS deliberately holding the CPU back to cool down.
  It is a symptom; never suggest "killing" it. Look at the other causes.
- **WindowServer** high = drawing the screen is expensive: scaled resolutions
  (a 4K panel driven at "looks like 2560x1440" renders 5120x2880 and downsamples
  every frame), several large displays, screen recording/sharing/agent screen
  control (`replayd`, `*ComputerUse*`), constant animations. The fix is in the
  display mode and the capturing app, not in WindowServer.
- **Full swap / compressor** with pressure "normal" means memory was short and
  is still paged out; it costs CPU when touched again. Quitting or restarting
  memory-heavy apps helps more than closing tiny ones.
- **fseventsd** high = many files changing at once (git checkouts/worktrees,
  `node_modules` installs, builds and watchers, sync clients, backups); Spotlight
  then indexes the same churn. Point at the bulk job, not at fseventsd.
- **spindump** = an app stopped responding and macOS is writing a hang report;
  name the app from `APP_HANG` evidence. `CPU_RESOURCE_REPORTS` lists processes
  macOS flagged for CPU bursts in the last 3 h - useful when it is not hot right now.
- **Spotlight, Photos analysis, Time Machine, iCloud, software update** are
  temporary background jobs; say so instead of recommending to kill them.
- `%CPU` is per core: 100% = one full core, a 10-core Mac has 1000%.
  The `CPU:` line (user/sys/idle) is for the whole machine.
- Electron apps (Slack, VS Code, Claude, ChatGPT, Chrome...) run many helper
  processes; judge them by the `apps` total, not one helper.

## Act only with consent

This skill does not change anything. If the user wants you to act:

- Quitting an app or killing a process: name the exact process, pid and owning
  app, and ask first. Never kill system processes (kernel_task, WindowServer,
  launchd, `*d` daemons) - that logs the user out or does nothing useful.
- Display resolution, Low Power Mode, Spotlight exclusions and other settings:
  tell the user where to change them (System Settings → Displays / Battery /
  Spotlight); do not change system settings yourself.
- Do not stop the agent session that is talking to the user, and do not stop
  other agents' sessions without asking.

## Optional: real temperatures (macOS)

macOS hides sensor temperatures from non-root processes. If the user wants
them, give them the command to run themselves:

```sh
sudo powermetrics --samplers smc,thermal -i 2000 -n 1
```

On Linux the script already reads `/sys/class/thermal` and reports
`HIGH_TEMPERATURE` at 80 °C and above.

## Watching over time

For "it heats up every afternoon" style problems, run the script a few times a
few minutes apart (or ask the user to run it when it is hot) and compare which
processes and findings repeat. Do not leave background loops running after the
conversation.
