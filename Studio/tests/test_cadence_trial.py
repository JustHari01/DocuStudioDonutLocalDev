from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from docurandom.cadence_trial import start_trial, reconcile_trial
from docurandom.workspace_scheduler import VIETNAM_TIMEZONE, load_schedule_shifts


class CadenceTrialTests(TestCase):
    def config(self):
        return {"schedule_default_chain_version": 4, "schedule_armed": True,
                "schedule_active_shift_id": "third", "schedule_active_stage": "retry",
                "protected": "unchanged", "schedule_shifts": [
                    {"shift_id": name, "workflow_at": f"21/10/2026 {work}",
                     "upload_at": f"22/10/2026 {public}", "workflow_enabled": True,
                     "upload_enabled": True, "workflow_daily": True, "upload_daily": True,
                     "workflow_claimed_for": "receipt", "upload_claimed_for": "receipt2"}
                    for name, work, public in [("first", "04:30", "08:30"),
                                               ("second", "08:30", "13:30"),
                                               ("third", "13:30", "04:30")]]}

    def test_28_slots_first_tomorrow_and_keeps_checkpoint(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp); original = self.config()
            config = start_trial(path, original, now=datetime(2026, 10, 21, 19, tzinfo=VIETNAM_TIMEZONE))
            trial = json.loads((path / "cadence-trial.json").read_text())
            self.assertEqual(trial["first_public"], "2026-10-22T13:30:00+07:00")
            self.assertEqual(trial["last_public"], "2026-11-18T13:30:00+07:00")
            self.assertEqual(len(load_schedule_shifts(config, fallback_workflow_at="", fallback_upload_at="")), 1)
            self.assertEqual(config["schedule_active_stage"], "retry")
            self.assertEqual(config["protected"], "unchanged")
            self.assertEqual(len(original["schedule_shifts"]), 3)
            # Day/claim progression must not count as a manual clock edit.
            config["schedule_shifts"][0]["upload_at"] = "18/11/2026 13:30"
            self.assertIs(reconcile_trial(path, config, now=datetime(2026, 11, 18, 15, tzinfo=VIETNAM_TIMEZONE)), config)
            restored = reconcile_trial(path, config, now=datetime(2026, 11, 19, tzinfo=VIETNAM_TIMEZONE))
            self.assertEqual(len(restored["schedule_shifts"]), 3)
            self.assertEqual(restored["schedule_shifts"][0]["upload_at"], "19/11/2026 08:30")
            self.assertEqual(restored["protected"], "unchanged")
            self.assertEqual(json.loads((path / "cadence-trial.json").read_text())["status"], "completed")

    def test_manual_edit_wins_and_other_workspace_untouched(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp); config = start_trial(path, self.config(), now=datetime(2026, 10, 21, 19, tzinfo=VIETNAM_TIMEZONE))
            config["schedule_shifts"][0]["upload_at"] = "22/10/2026 16:30"
            result = reconcile_trial(path, config, now=datetime(2026, 11, 19, tzinfo=VIETNAM_TIMEZONE))
            self.assertIs(result, config)
            self.assertEqual(json.loads((path / "cadence-trial.json").read_text())["status"], "operator_override")
            self.assertIs(reconcile_trial(path / "another", config, now=datetime.now(VIETNAM_TIMEZONE)), config)
