from contextlib import closing
from pathlib import Path
import sqlite3

from psycopg import sql

from .storage import BookingStore


TABLE_COLUMNS = {
    "availability": (
        "id", "starts_at", "ends_at", "hourly_price", "capacity", "status", "created_by", "created_at",
    ),
    "bookings": (
        "id", "window_id", "user_id", "full_name", "username", "car_plate", "starts_at", "ends_at",
        "hours", "hourly_price", "total_price", "status", "request_key", "created_at", "cancelled_at",
    ),
}


def migrate_sqlite(store: BookingStore, path: Path) -> dict[str, int]:
    """Копирует SQLite в пустую PostgreSQL одной транзакцией, не изменяя источник."""
    source_path = path.resolve()
    if not source_path.is_file():
        raise ValueError(f"Файл SQLite не найден: {source_path}")

    with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as source:
        source.row_factory = sqlite3.Row
        source.execute("BEGIN")
        tables = {row["name"] for row in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not TABLE_COLUMNS.keys() <= tables:
            raise ValueError("В SQLite не найдены таблицы availability и bookings.")

        store.initialize()
        with store.connect() as target:
            target.execute("LOCK TABLE availability, bookings, track_info IN ACCESS EXCLUSIVE MODE")
            occupied = target.execute("""
                SELECT EXISTS (SELECT 1 FROM availability)
                    OR EXISTS (SELECT 1 FROM bookings)
                    OR EXISTS (SELECT 1 FROM track_info WHERE rules_text <> '' OR directions_url <> '') AS occupied
            """).fetchone()["occupied"]
            if occupied:
                raise ValueError("Перенос возможен только в пустую PostgreSQL: данные уже существуют.")

            counts = {}
            for table, columns in TABLE_COLUMNS.items():
                # Имена таблиц заданы константами; пользовательские значения идут параметрами.
                rows = source.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                values = []
                for row in rows:
                    record = dict(row)
                    record.setdefault("capacity", 1)
                    values.append(tuple(record[column] for column in columns))
                query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    sql.Identifier(table),
                    sql.SQL(", ").join(map(sql.Identifier, columns)),
                    sql.SQL(", ").join(sql.Placeholder() for _ in columns),
                )
                with target.cursor() as cursor:
                    cursor.executemany(query, values)
                counts[table] = len(rows)

                # Учитываем и удалённые ID, чтобы новые записи не переиспользовали их.
                last_id = max((row["id"] for row in rows), default=0)
                if "sqlite_sequence" in tables:
                    sequence = source.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).fetchone()
                    if sequence:
                        last_id = max(last_id, sequence["seq"])
                target.execute(sql.SQL("ALTER TABLE {} ALTER COLUMN id RESTART WITH {}").format(
                    sql.Identifier(table), sql.Literal(last_id + 1),
                ))

            counts["track_info"] = 0
            if "track_info" in tables:
                info = source.execute("SELECT rules_text, directions_url FROM track_info WHERE id = 1").fetchone()
                if info:
                    target.execute(
                        "UPDATE track_info SET rules_text = %s, directions_url = %s WHERE id = 1",
                        (info["rules_text"], info["directions_url"]),
                    )
                    counts["track_info"] = 1
            return counts
