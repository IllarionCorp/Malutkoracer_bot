def database_error_hint(error: BaseException) -> str:
    """Объясняет ошибку подключения, не выводя DSN, пароль или текст запроса."""
    sqlstate = getattr(error, "sqlstate", None)
    by_sqlstate = {
        "28P01": "Не прошла проверка пароля. Проверь пароль пользователя PostgreSQL в DATABASE_URL.",
        "3D000": "База данных не существует. Создай базу или исправь её имя в DATABASE_URL.",
        "42501": "Недостаточно прав. Проверь права пользователя PostgreSQL на базу, схему и таблицы.",
        "3F000": "Схема не существует. Проверь схему и search_path пользователя PostgreSQL.",
        "53300": "Достигнут лимит подключений PostgreSQL. Проверь активные подключения на сервере.",
        "57P03": "PostgreSQL временно не принимает подключения. Проверь состояние сервера.",
    }
    if sqlstate in by_sqlstate:
        return by_sqlstate[sqlstate]

    # Ошибки до установления соединения часто приходят без SQLSTATE.
    message = str(error).lower()
    if "password authentication failed" in message or "no password supplied" in message:
        return by_sqlstate["28P01"]
    if "no pg_hba.conf entry" in message:
        return "Подключение не разрешено в pg_hba.conf. Проверь правило для адреса, пользователя и базы."
    if "does not exist" in message:
        if 'database "' in message:
            return by_sqlstate["3D000"]
        if 'role "' in message:
            return "Пользователь PostgreSQL не существует. Создай роль с LOGIN или исправь пользователя в DATABASE_URL."
    if "permission denied" in message:
        return by_sqlstate["42501"]
    if "connection refused" in message or "actively refused" in message:
        return "Сервер не принимает соединение по указанному адресу и порту. Проверь PostgreSQL и DATABASE_URL."
    if "timeout" in message or "timed out" in message:
        return "Истекло время подключения. Проверь адрес, порт и сетевую доступность PostgreSQL."
    if any(text in message for text in (
        "could not translate host name", "name or service not known", "getaddrinfo failed",
        "nodename nor servname provided", "failed to resolve host",
    )):
        return "Не удалось определить адрес сервера. Проверь имя хоста в DATABASE_URL и DNS."
    if "invalid percent-encoded token" in message or "invalid uri query parameter" in message:
        return "Некорректный DATABASE_URL. Проверь параметры URL и кодирование спецсимволов в пароле."
    return "Проверь DATABASE_URL, доступность базы и права пользователя. Проверь подключение через psql."
