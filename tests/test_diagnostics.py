import unittest

from booking_bot.diagnostics import database_error_hint


class DatabaseError(Exception):
    def __init__(self, message, *, sqlstate=None):
        super().__init__(message)
        self.sqlstate = sqlstate


class DatabaseDiagnosticsTests(unittest.TestCase):
    def test_connection_errors_without_sqlstate(self):
        cases = (
            ('connection failed: FATAL: password authentication failed for user "booking_bot"', "проверка пароля"),
            ("connection failed: fe_sendauth: no password supplied", "проверка пароля"),
            ('FATAL: database "booking_bot" does not exist', "База данных не существует"),
            ('FATAL: role "booking_bot" does not exist', "Пользователь PostgreSQL не существует"),
            ('FATAL: no pg_hba.conf entry for host "127.0.0.1"', "pg_hba.conf"),
            ("connection to server failed: Connection refused", "не принимает соединение"),
            ("connection failed: timeout expired", "Истекло время"),
            ('could not translate host name "db" to address: Name or service not known', "имя хоста"),
            ("failed to resolve host 'db': [Errno 11001] getaddrinfo failed", "имя хоста"),
            ("permission denied for schema public", "Недостаточно прав"),
            ('invalid percent-encoded token: "secret%password"', "кодирование спецсимволов"),
        )
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertIn(expected, database_error_hint(DatabaseError(message)))

    def test_sqlstate_does_not_depend_on_server_message_language(self):
        cases = {
            "28P01": "проверка пароля", "3D000": "База данных не существует",
            "42501": "Недостаточно прав", "3F000": "Схема не существует",
            "53300": "лимит подключений", "57P03": "временно не принимает подключения",
        }
        for sqlstate, expected in cases.items():
            with self.subTest(sqlstate=sqlstate):
                self.assertIn(expected, database_error_hint(DatabaseError("Ошибка сервера", sqlstate=sqlstate)))

    def test_diagnostics_never_echo_credentials_or_query_text(self):
        messages = (
            "postgresql://user:secret_password@localhost/db failed",
            'password authentication failed for user "secret_password"',
            'invalid percent-encoded token: "secret_password"',
            "SELECT 'secret_password' failed: permission denied",
        )
        for message in messages:
            with self.subTest(message=message):
                hint = database_error_hint(DatabaseError(message))
                self.assertNotIn("secret_password", hint)
                self.assertNotIn("postgresql://", hint)
                self.assertNotIn("SELECT", hint)

    def test_unknown_error_has_actionable_fallback(self):
        self.assertIn("psql", database_error_hint(DatabaseError("Неизвестная ошибка")))


if __name__ == "__main__":
    unittest.main()
