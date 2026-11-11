import json
import unittest
from unittest.mock import patch

import test_claude_statusline_windows as window_tests
from test_agentcat import agentcat
import agentcat_local_experience as experience


class SessionProjectionTests(unittest.TestCase):
    def test_wait_continue_stop_and_expiry(self):
        events = [experience.session_observation("PermissionRequest", {}, "opaque", 100)]
        self.assertEqual(experience.session_summary(events, 101)["waiting"], 1)
        events.append(experience.session_observation("PostToolUse", {}, "opaque", 102))
        result = experience.session_summary(events, 103)
        self.assertEqual((result["waiting"], result["running"]), (0, 1))
        events.append(experience.session_observation("Stop", {}, "opaque", 104))
        self.assertEqual(experience.session_summary(events, 104)["completed"], 1)
        self.assertEqual(experience.session_summary(events, 705)["completed"], 0)

    def test_failed_tool_continuation_and_session_end_clear_waiting(self):
        for event, state in (("PostToolUseFailure", "running"), ("SessionEnd", "completed")):
            events = [experience.session_observation("PermissionRequest", {}, "opaque", 100),
                      experience.session_observation(event, {}, "opaque", 101)]
            result = experience.session_summary(events, 102)
            self.assertEqual(result["waiting"], 0)
            self.assertEqual(result[state], 1)

    def test_concurrent_sessions_are_independent_and_future_events_rejected(self):
        events = [experience.session_observation("PermissionRequest", {}, "first", 100),
                  experience.session_observation("Stop", {}, "second", 100),
                  experience.session_observation("PermissionRequest", {}, "future", 200)]
        result = experience.session_summary(events, 101)
        self.assertEqual((result["waiting"], result["completed"]), (1, 1))
        self.assertNotIn("first", json.dumps(result))

    def test_only_known_notification_types_change_state(self):
        self.assertIsNone(experience.session_observation("Notification", {"message": "permission needed"}, "opaque", 100))
        self.assertEqual(experience.session_observation("Notification", {"notification_type": "permission_prompt"}, "opaque", 100)["state"], "waiting")


class HookStorageTests(unittest.TestCase):
    # Reuse the sandbox lifecycle without inheriting its test methods.
    setUp = window_tests.ClaudeStatuslineWindowTests.setUp
    account = window_tests.ClaudeStatuslineWindowTests.account

    def test_hook_projection_is_collector_owned_and_snapshot_has_no_session_id(self):
        with patch.object(agentcat.time, "time", return_value=1000):
            agentcat.store_event("claude", "claude-hook", "PermissionRequest", {
                "session_id": "private-session", "prompt": "private prompt", "cwd": "/private/repo",
                "_agentcat_session": {"session": "forged", "state": "completed", "observedAt": 1000},
            })
            result = agentcat.local_session_summary()
        self.assertEqual(result["waiting"], 1)
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("forged", json.dumps(result))
