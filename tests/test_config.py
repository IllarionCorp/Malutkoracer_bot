import os
import unittest
from unittest.mock import patch

from booking_bot.config import Settings


class SettingsTests(unittest.TestCase):
    def load(self, **environment):
        with patch.dict(os.environ, environment, clear=True), patch("booking_bot.config.load_dotenv"):
            return Settings.from_env(require_token=False)

    def test_postgresql_url(self):
        url = "postgresql://booking_bot:secret@localhost:5432/booking_bot"
        settings = self.load(DATABASE_URL=url)
        self.assertEqual(settings.database_url, url)
        self.assertNotIn(url, repr(settings))
        self.assertNotIn("secret", repr(settings))

    def test_postgres_alias(self):
        url = "postgres://localhost/booking_bot?sslmode=require"
        self.assertEqual(self.load(DATABASE_URL=url).database_url, url)

    def test_missing_url_does_not_fall_back_to_sqlite(self):
        with self.assertRaisesRegex(ValueError, "DATABASE_URL"):
            self.load(DATABASE_PATH="data/bookings.sqlite3")

    def test_rejects_other_databases_without_showing_credentials(self):
        for url in ("sqlite:///bookings.db", "mysql://user:secret@localhost/bot", ""):
            with self.subTest(url=url), self.assertRaises(ValueError) as raised:
                self.load(DATABASE_URL=url)
            self.assertNotIn("secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
