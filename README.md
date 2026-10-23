# Bedtime

Bedtime turns the **bed** heater off 15 minutes before a Voron 2.4 print finishes, so the sheet cools and the part releases. The hotend stays at temperature until the print actually ends.

It is a Klipper extra. Mainsail (or Fluidd) does not need a plugin. Once the extra is installed, Mainsail shows four buttons: arm, disarm, off now, and status.

## How it knows when the print will end

Modern slicers write a remaining-time map into the gcode:

- **OrcaSlicer, PrusaSlicer, SuperSlicer** emit `M73 P… R…` about once a minute. `R` is minutes left at the slicer's speeds.
- **Cura** writes `;TIME:` (total seconds) and `;TIME_ELAPSED:` at each layer.

When a print starts, Bedtime reads that map in the background. During the print it compares Klipper's file position with the map. The speed slider counts: at 200% the bed turns off earlier in the file, at 50% it waits, so a slow print does not lose adhesion early. Pause freezes the decision. Resume continues from the same place in the file.

`PRINT_START` time does not count. Quad gantry level, mesh, and heat soak run inside one macro call while the file position stays put, and the shutoff is based on the sliced toolpath rather than the wall clock since you pressed print.

If a file has none of those marks, Bedtime falls back to the footer line `estimated printing time (normal mode)` and the fraction of the file that is real toolpath, and it says so in the console. That estimate is rough. Turn on M73.

Prints shorter than `lead_time + min_elapsed` (default 15 min + 2 min) leave the bed on. A print under about 17 minutes is not long enough to cool the bed that early without risking the first layers.

## Install

Copy this directory to the printer host (MainsailOS or a KIAUH install, the machine where `~/klipper` lives), then:

```bash
bash ~/bedtime/scripts/install.sh
sudo systemctl restart klipper
```

By hand, the same steps are:

```bash
ln -sfn ~/bedtime/bedtime.py ~/klipper/klippy/extras/bedtime.py
cp ~/bedtime/bedtime.cfg ~/printer_data/config/bedtime.cfg
sudo systemctl restart klipper
```

Add this line to `printer.cfg` if it is not already there:

```ini
[include bedtime.cfg]
```

The default section:

```ini
[bedtime]
lead_time: 900
heater: heater_bed
target_temp: 0
min_elapsed: 120
keep_off: True
```

`lead_time` is seconds of cooling before the last move. `target_temp: 0` switches the bed off. Set it to something like `35` if you would rather hold a release temperature than let the bed keep falling. `keep_off` puts the target back if a later `M140` in the file turns the bed on again. `BEDTIME_DISABLE` stops that.

Restart Klipper from Mainsail after the include. The next print should log a line like:

```text
Bedtime: bed turns off 15 min before the end (slicer estimate 2 h 14 min, from M73).
```

When the lead is reached, the bed target in Mainsail drops and the console says the bed was set with about that much time left. `PRINT_END` can still turn the heaters off at the real end. That is fine.

### Slicer

OrcaSlicer, which most Voron 2.4 profiles use:

- G-code flavor: **Klipper**
- Leave **Disable set remaining print time** unchecked

PrusaSlicer and SuperSlicer:

- Printer Settings, General, Firmware: **Supports remaining times** on

Cura needs no extra setting. It already writes `;TIME:` and `;TIME_ELAPSED:`.

If a print dies on `Unknown command: M73`, Klipper has nothing registered to accept that command. Add this to `printer.cfg`:

```ini
[display_status]
```

That is stock Klipper. Bedtime does not replace `M73`, so the progress percentage Mainsail already shows keeps working.

### Update manager

Optional `moonraker.conf` snippet, once this directory is a git checkout with a remote:

```ini
[update_manager bedtime]
type: git_repo
primary_branch: main
path: ~/bedtime
origin: https://github.com/YOU/bedtime.git
managed_services: klipper
is_system_service: False
```

The symlink into `klippy/extras` keeps pointing at `~/bedtime/bedtime.py`, so a pull is enough. Restart Klipper after an update.

## Tuning for a Voron 2.4

A Voron bed is a thick plate, a magnet, and PEI, inside a box that holds heat. The default lead is 15 minutes. That is enough for the bed to actually fall, including from a 100°C ABS set point. It will still not reach room temperature if the chamber is hot.

| Material | Typical bed | Lead to try |
| --- | --- | --- |
| PLA | 60°C | 15 min (`900`), less only if the part releases early |
| PETG | 70–80°C | 15–20 min (`900`–`1200`) |
| ABS / ASA | 100–110°C, doors closed | 15–25 min (`900`–`1500`) |

Change it live, including mid-print:

```text
BEDTIME_LEAD MINUTES=8
```

If the part is still stuck when you open the door, raise the lead. If the part lets go while the toolhead is still printing, lower it. A loose part on a CoreXY can be caught by the nozzle.

Opening the door, or running the exhaust, near the end dumps chamber heat and helps more than another minute of lead on an ABS print.

`min_elapsed` (default 120) is slicer-time into the toolpath, not time since you pressed print. Do not drop it to zero. It is what stops a bad estimate from killing bed adhesion during the first layers.

## Commands

| Console | Mainsail button | What it does |
| --- | --- | --- |
| `BEDTIME_ENABLE` | `BEDTIME_ARM` | Arm early shutoff. This is the default. |
| `BEDTIME_DISABLE` | `BEDTIME_DISARM` | Leave the bed as it is until the print ends. |
| `BEDTIME_NOW` | `BEDTIME_OFF_NOW` | Set the bed to `target_temp` immediately. |
| `BEDTIME_STATUS` | `BEDTIME_INFO` | Lead, estimate source, time left. |
| `BEDTIME_LEAD MINUTES=5` | | Change the lead without a restart. `SECONDS=` also works. |

Arm and disarm stick until you flip them or restart Klipper. They are not per-print.

## What it will not do

- It does not turn off the hotend, the chamber heater, or the fans.
- It does not wait until the bed has reached a temperature before ending the print. It only moves the shutoff earlier, so the bed is already falling when the toolhead stops.
- M73 is integer minutes, so the shutoff can be up to about a minute off the lead you asked for. Layer-time files from Cura are finer. The file-size fallback is coarser than either.
- Slicer time is not Klipper time. If the slicer's acceleration is far from the printer's, the "few minutes" will drift. Match the printer limits in the slicer, and use the speed slider rather than expecting Bedtime to notice a one-off `M204`.
# bedtime
