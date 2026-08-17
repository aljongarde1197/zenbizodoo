import os
import unittest
from unittest.mock import patch

from app.config import ConfigurationError, Settings


class OAuthConfigTests(unittest.TestCase):
    def base(self):
        return {
            "ODOO_URL": "https://odoo.example.com",
            "ODOO_DATABASE": "staging",
            "ODOO_API_KEY": "key",
            "TRANSPORT": "streamable-http",
        }

    def test_disabled(self):
        with patch.dict(
            os.environ,
            self.base() | {"AUTH_ENABLED": "false"},
            clear=True,
        ):
            self.assertFalse(Settings.from_env().auth_enabled)

    def test_enabled_requires_values(self):
        with patch.dict(
            os.environ,
            self.base() | {"AUTH_ENABLED": "true"},
            clear=True,
        ):
            with self.assertRaises(ConfigurationError):
                Settings.from_env()

    def test_enabled_complete(self):
        env = self.base() | {
            "AUTH_ENABLED": "true",
            "AUTH_ISSUER_URL": "https://auth.example.com",
            "AUTH_RESOURCE_SERVER_URL": "https://mcp.example.com/mcp",
            "AUTH_JWKS_URL": "https://auth.example.com/jwks",
            "AUTH_AUDIENCE": "https://mcp.example.com/mcp",
            "AUTH_REQUIRED_SCOPES": "odoo.read",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()
            self.assertTrue(settings.auth_enabled)
            self.assertEqual(settings.auth_required_scopes, ["odoo.read"])


if __name__ == "__main__":
    unittest.main()
