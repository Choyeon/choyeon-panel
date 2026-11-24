"""API Key invariants that do not require a Linux systemd host."""

import os
import tempfile
import unittest

SESSION_DIR = tempfile.mkdtemp(prefix="cp_agent_api_")
os.environ.setdefault("CP_DATA_DIR", SESSION_DIR)

from app import agent_auth  # noqa: E402
from app import database as dbm  # noqa: E402


class AgentKeyTests(unittest.TestCase):
    def setUp(self):
        dbm.execute("DELETE FROM api_keys")

    def test_secret_is_returned_once_and_not_in_listing(self):
        created = agent_auth.create_key("deploy-bot", ["system:read", "deploy:write"], 30, "tester")
        self.assertTrue(created["token"].startswith("cp_live_"))
        self.assertNotIn("token", agent_auth.list_keys()[0])
        auth = agent_auth.authenticate(created["token"])
        self.assertEqual(auth["public_id"], created["public_id"])
        self.assertEqual(auth["scopes"], {"system:read", "deploy:write"})

    def test_revoke_invalidates_existing_secret(self):
        created = agent_auth.create_key("bot", ["apps:read"], 30, "tester")
        self.assertIsNotNone(agent_auth.authenticate(created["token"]))
        agent_auth.revoke_key(created["id"], "tester")
        self.assertIsNone(agent_auth.authenticate(created["token"]))

    def test_unknown_scope_is_rejected(self):
        with self.assertRaises(ValueError):
            agent_auth.create_key("bot", ["root:shell"], 30, "tester")

    def test_write_rate_limit_is_separate_from_read_limit(self):
        created = agent_auth.create_key("bot", ["system:read"], 30, "tester")
        for _ in range(10):
            self.assertFalse(agent_auth.rate_limited(created["id"], "POST"))
        self.assertTrue(agent_auth.rate_limited(created["id"], "POST"))
        self.assertFalse(agent_auth.rate_limited(created["id"], "GET"))


if __name__ == "__main__":
    unittest.main()
