"""
CSV-driven lamp schedule.

The existing device_control.yaml automation forwards the .csv values to the lamp over MQTT,
so this app replaces only the *scheduling* layer (Lamp_control), not the transport.

Day selection: grow day = first_day + days elapsed since start_date, clamped to [first_day, last_day].
After last_day, the last day's file repeats indefinitely.

IMPORTANT: the chamber's Lamp_control toggle (input_boolean.lamp1_control_toggle) must be OFF while 
this app's toggle is ON, otherwise the two controllers overwrite each other.
"""

import csv
import os
from datetime import date, datetime, timedelta

import appdaemon.plugins.hass.hassapi as hass

SLOT_MINUTES = 15
SLOTS_PER_DAY = 96


class LampCsvSchedule(hass.Hass):

    def initialize(self):
        self.label = self.args.get("label", "Lamp CSV Schedule")
        self.toggle_id = self.args["toggle_id"]
        # Mapping: CSV column name -> HA entity_id
        self.channel_ids = self.args["channel_ids"]
        self.start_date = datetime.strptime(
            self.args["start_date"], "%Y-%m-%d"
        ).date()
        self.first_day = int(self.args.get("first_day", 1)) # to be modified
        self.last_day = int(self.args.get("last_day", 21)) # to be modified
        self.csv_file = self.args["csv_file"]
        default_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "lamp_csv_schedules"
        )
        self.csv_dir = self.args.get("csv_dir", default_dir)

        self.apply_current_slot()

        now = datetime.now()
        next_quarter = (now.minute // SLOT_MINUTES + 1) * SLOT_MINUTES
        next_tick = now.replace(minute=0, second=0, microsecond=0) + timedelta(
            minutes=next_quarter
        )
        self.run_every(self.tick, next_tick, SLOT_MINUTES * 60)

        self.listen_state(self.toggle_changed, self.toggle_id)

        self.log(
            f"[{self.label}] Initialized. csv_dir={self.csv_dir}, "
            f"start_date={self.start_date}, days {self.first_day}-{self.last_day}"
        )

    # ------------------------------------------------------------------ #
    # Callbacks
    # ------------------------------------------------------------------ #
    def toggle_changed(self, entity, attribute, old, new, kwargs):
        if new == "on":
            self.log(f"[{self.label}] Toggle on, applying current slot.")
            self.apply_current_slot()
        else:
            # Intentionally do nothing on 'off': the lamp keeps its last
            # values until Lamp_control (or the operator) takes over.
            self.log(f"[{self.label}] Toggle off, schedule paused.")

    def tick(self, kwargs):
        self.apply_current_slot()

    # ------------------------------------------------------------------ #
    # Core logic
    # ------------------------------------------------------------------ #
    def current_day_index(self):
        elapsed = (date.today() - self.start_date).days
        idx = self.first_day + max(elapsed, 0)
        return min(idx, self.last_day)

    def load_day_file(self, day_index):
        """
        Combined file: 96-row blocks in grow-day order (day 1 = first block),
        so grow day N is rows [(N-1)*96 : N*96]. Verified against the per-day
        extracts. If the file is regenerated, keep this layout or the positional
        slice will read the wrong day.
        """
        path = os.path.join(self.csv_dir, self.csv_file)
        with open(path, newline="") as fh:
            all_rows = list(csv.DictReader(fh, delimiter=";"))
        start = (day_index - 1) * SLOTS_PER_DAY
        block = all_rows[start:start + SLOTS_PER_DAY]
        if len(block) < SLOTS_PER_DAY:
            raise ValueError(
                f"combined CSV lacks a full block for grow day {day_index} "
                f"(need rows {start}-{start + SLOTS_PER_DAY}, file has {len(all_rows)})"
            )
        if block[0]["Lamp timestamp"].strip() != "00:00":
            raise ValueError(
                f"combined CSV block for grow day {day_index} does not start at "
                f"00:00 (got {block[0]['Lamp timestamp']}); unexpected file layout"
            )
        return {r["Lamp timestamp"].strip(): r for r in block}

    def apply_current_slot(self):
        if self.get_state(self.toggle_id) != "on":
            return

        day_index = self.current_day_index()
        try:
            rows = self.load_day_file(day_index)
        except FileNotFoundError as exc:
            self.log(f"[{self.label}] CSV file not found: {exc}", level="ERROR")
            return
        except (KeyError, ValueError) as exc:
            self.log(f"[{self.label}] CSV parse error: {exc}", level="ERROR")
            return

        # Floor 'now' to the 15-min slot; if that slot is missing in the
        # file, walk backwards (wrapping past midnight) to the most recent
        # available slot (defensive only; current files have all 96 slots).
        now = datetime.now()
        slot = now.replace(
            minute=(now.minute // SLOT_MINUTES) * SLOT_MINUTES,
            second=0,
            microsecond=0,
        )
        row = None
        for _ in range(SLOTS_PER_DAY):
            key = slot.strftime("%H:%M")
            if key in rows:
                row = rows[key]
                break
            slot -= timedelta(minutes=SLOT_MINUTES)
        if row is None:
            self.log(f"[{self.label}] No usable slot in day {day_index} file.",
                     level="ERROR")
            return

        applied = []
        for column, entity_id in self.channel_ids.items():
            try:
                value = float(row[column])
            except (KeyError, ValueError) as exc:
                self.log(
                    f"[{self.label}] Bad value for column '{column}' "
                    f"in slot {row['Lamp timestamp']}: {exc}",
                    level="ERROR",
                )
                continue
            self.call_service(
                "input_number/set_value", entity_id=entity_id, value=value
            )
            applied.append(f"{column}={value:g}")

        self.log(
            f"[{self.label}] Day {day_index}, slot "
            f"{row['Lamp timestamp']}: " + ", ".join(applied)
        )
