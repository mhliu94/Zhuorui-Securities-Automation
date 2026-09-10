from datetime import datetime, timezone
import json
import unittest

from zhuorui.api.errors import ApiError
from zhuorui.api.recovery import BEIJING, RecoverySchedule, login_delay_seconds


def beijing(hour, minute=0, second=0):
    return datetime(2026, 9, 11, hour, minute, second, tzinfo=BEIJING)


class LoginDelayTests(unittest.TestCase):
    def test_exact_beijing_window_boundaries(self):
        for instant, expected in ((beijing(8, 59, 59), 0), (beijing(9), 300),
                                  (beijing(15, 59, 59), 300), (beijing(16), 0),
                                  (beijing(0), 0), (beijing(23, 59, 59), 0)):
            with self.subTest(instant=instant):
                self.assertEqual(login_delay_seconds(instant), expected)

    def test_utc_and_epoch_inputs_match_beijing_detection(self):
        instant = beijing(9)
        self.assertEqual(login_delay_seconds(instant.astimezone(timezone.utc)), 300)
        self.assertEqual(login_delay_seconds(instant.timestamp()), 300)
        self.assertEqual(login_delay_seconds(datetime(2026, 9, 10, 20, tzinfo=timezone.utc)), 0)

    def test_invalid_or_ambiguous_time_is_rejected(self):
        for instant in (datetime(2026, 9, 11, 9), True, "2026-09-11T09:00:00", float("nan"), float("inf"), 10 ** 400):
            with self.subTest(instant=instant), self.assertRaises(ApiError):
                login_delay_seconds(instant)


class RecoveryScheduleTests(unittest.TestCase):
    def test_first_detection_is_retained_and_repeat_failures_do_not_move_deadline(self):
        schedule = RecoverySchedule()
        detected = beijing(10).timestamp()
        self.assertTrue(schedule.observe_logout(detected, "session_expired"))
        self.assertEqual(schedule.due_at, detected + 300)
        self.assertFalse(schedule.observe_logout(detected + 290, "logged_in_elsewhere"))
        self.assertEqual(schedule.detected_at, detected)
        self.assertEqual(schedule.due_at, detected + 300)
        self.assertEqual(schedule.reason, "session_expired")
        self.assertFalse(schedule.ready(detected + 299.999))
        self.assertTrue(schedule.ready(detected + 300))

    def test_detection_before_1600_keeps_full_five_minutes_across_boundary(self):
        schedule = RecoverySchedule()
        detected = beijing(15, 59).timestamp()
        schedule.observe_logout(detected)
        self.assertEqual(schedule.due_at, beijing(16, 4).timestamp())
        self.assertFalse(schedule.ready(beijing(16).timestamp()))

    def test_outside_window_is_immediate_even_if_next_observation_is_inside(self):
        schedule = RecoverySchedule()
        detected = beijing(8, 59).timestamp()
        schedule.observe_logout(detected)
        self.assertTrue(schedule.ready(detected))
        schedule.observe_logout(beijing(9).timestamp())
        self.assertEqual(schedule.due_at, detected)

    def test_retry_is_reserved_before_attempt_and_failure_waits_from_completion(self):
        schedule = RecoverySchedule(retry_seconds=120)
        detected = beijing(16).timestamp()
        schedule.observe_logout(detected)
        self.assertTrue(schedule.start_attempt(detected))
        self.assertTrue(schedule.in_progress)
        self.assertEqual(schedule.attempts, 1)
        self.assertEqual(schedule.due_at, detected + 120)
        self.assertFalse(schedule.start_attempt(detected + 121))
        schedule.failed(detected + 10)
        self.assertFalse(schedule.in_progress)
        self.assertEqual(schedule.due_at, detected + 130)
        self.assertFalse(schedule.ready(detected + 129))
        self.assertTrue(schedule.start_attempt(detected + 130))
        self.assertEqual(schedule.attempts, 2)

    def test_start_before_due_has_no_side_effects(self):
        schedule = RecoverySchedule()
        schedule.observe_logout(beijing(9))
        saved = schedule.to_dict()
        self.assertFalse(schedule.start_attempt(beijing(9, 4, 59)))
        self.assertEqual(schedule.to_dict(), saved)

    def test_success_clears_episode_and_next_logout_gets_new_detection_policy(self):
        schedule = RecoverySchedule()
        schedule.observe_logout(beijing(9))
        schedule.start_attempt(beijing(9, 5))
        schedule.succeeded()
        self.assertEqual(schedule.to_dict(), RecoverySchedule().to_dict())
        self.assertFalse(schedule.ready(beijing(10)))
        self.assertTrue(schedule.observe_logout(beijing(16)))
        self.assertTrue(schedule.ready(beijing(16)))

    def test_credential_or_verification_block_survives_until_explicit_unblock(self):
        for reason in ("credentials", "verification_required"):
            with self.subTest(reason=reason):
                schedule = RecoverySchedule()
                detected = beijing(16).timestamp()
                schedule.observe_logout(detected)
                schedule.start_attempt(detected)
                schedule.failed(detected + 5, blocked_reason=reason)
                self.assertFalse(schedule.ready(detected + 10000))
                schedule = RecoverySchedule.from_dict(schedule.to_dict())
                self.assertFalse(schedule.ready(detected + 10000))
                schedule.unblock()
                self.assertTrue(schedule.ready(detected + 10000))

    def test_delayed_schedule_roundtrips_json_without_resetting_first_detection(self):
        schedule = RecoverySchedule()
        schedule.observe_logout(beijing(9))
        saved = json.loads(json.dumps(schedule.to_dict(), allow_nan=False))
        restored = RecoverySchedule.from_dict(saved)
        self.assertEqual(restored.to_dict(), saved)
        self.assertFalse(restored.observe_logout(beijing(9, 4)))
        self.assertTrue(restored.ready(beijing(9, 5)))

    def test_restart_after_interrupted_attempt_keeps_reserved_retry_deadline(self):
        schedule = RecoverySchedule()
        detected = beijing(16).timestamp()
        schedule.observe_logout(detected)
        schedule.start_attempt(detected)
        restored = RecoverySchedule.from_dict(schedule.to_dict())
        self.assertFalse(restored.in_progress)
        self.assertEqual(restored.attempts, 1)
        self.assertFalse(restored.ready(detected + 299))
        self.assertTrue(restored.ready(detected + 300))

    def test_new_retry_configuration_does_not_change_already_persisted_deadline(self):
        schedule = RecoverySchedule(retry_seconds=300)
        detected = beijing(16).timestamp()
        schedule.observe_logout(detected)
        schedule.start_attempt(detected)
        schedule.failed(detected + 2)
        restored = RecoverySchedule.from_dict(schedule.to_dict(), retry_seconds=60)
        self.assertEqual(restored.due_at, detected + 302)
        restored.start_attempt(detected + 302)
        self.assertEqual(restored.due_at, detected + 362)

    def test_clock_moving_backward_does_not_make_retry_precede_previous_attempt(self):
        schedule = RecoverySchedule()
        detected = beijing(16).timestamp()
        schedule.observe_logout(detected)
        schedule.start_attempt(detected)
        schedule.failed(detected - 10)
        self.assertEqual(schedule.due_at, detected + 300)

    def test_invalid_intervals_reasons_or_failures_are_rejected(self):
        for value in (0, -1, True, float("nan"), float("inf"), "300"):
            with self.subTest(value=value), self.assertRaises(ApiError):
                RecoverySchedule(retry_seconds=value)
        with self.assertRaises(ApiError):
            RecoverySchedule().observe_logout(beijing(9), "private broker body")
        with self.assertRaises(ApiError):
            RecoverySchedule().failed(beijing(9))

    def test_malformed_or_contradictory_saved_state_is_rejected(self):
        schedule = RecoverySchedule()
        schedule.observe_logout(beijing(9))
        saved = schedule.to_dict()
        changes = ({"version": True}, {"version": 2}, {"attempts": True},
                   {"due_at": saved["detected_at"] - 1}, {"due_at": saved["detected_at"]},
                   {"reason": "secret"}, {"reason": []}, {"blocked_reason": {}},
                   {"detected_at": None}, {"in_progress": True})
        for values in changes:
            with self.subTest(values=values), self.assertRaises(ApiError):
                RecoverySchedule.from_dict({**saved, **values})
        with self.assertRaises(ApiError):
            RecoverySchedule.from_dict({})


if __name__ == "__main__":
    unittest.main()
