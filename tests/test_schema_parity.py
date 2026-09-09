"""Schema parity and real SQLite migration tests for PR #3."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from database import Base, engine
import models  # noqa: F401  (registers the tables in Base.metadata)


BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"


def _create_legacy_database(path: Path) -> dict:
    """Create the pre-PR #3 schema with representative, linked data."""
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;

        CREATE TABLE users (
            id INTEGER NOT NULL,
            name VARCHAR NOT NULL,
            email VARCHAR NOT NULL,
            hashed_password VARCHAR NOT NULL,
            role VARCHAR,
            PRIMARY KEY (id)
        );
        CREATE UNIQUE INDEX ix_users_email ON users (email);
        CREATE INDEX ix_users_id ON users (id);

        CREATE TABLE farms (
            id INTEGER NOT NULL,
            name VARCHAR NOT NULL,
            city VARCHAR NOT NULL,
            total_area FLOAT NOT NULL,
            latitude FLOAT,
            longitude FLOAT,
            PRIMARY KEY (id)
        );
        CREATE INDEX ix_farms_id ON farms (id);

        CREATE TABLE talhoes (
            id INTEGER NOT NULL,
            farm_id INTEGER,
            name VARCHAR NOT NULL,
            area FLOAT NOT NULL,
            crop VARCHAR,
            latitude FLOAT,
            longitude FLOAT,
            kml_coordinates VARCHAR,
            PRIMARY KEY (id),
            FOREIGN KEY (farm_id) REFERENCES farms (id)
        );
        CREATE INDEX ix_talhoes_id ON talhoes (id);

        INSERT INTO users (id, name, email, hashed_password, role) VALUES
            (1, 'Admin Demo', 'admin@orion.com', 'admin-hash', 'admin'),
            (2, 'Produtor', 'produtor@example.com', 'user-hash', 'Produtor Rural');

        INSERT INTO farms (id, name, city, total_area, latitude, longitude) VALUES
            (1, 'Fazenda Demo', 'Dourados', 100.5, -22.7, -55.5),
            (2, 'Fazenda Privada', 'Ponta Pora', 55.25, -22.6, -55.7);

        INSERT INTO talhoes
            (id, farm_id, name, area, crop, latitude, longitude, kml_coordinates)
        VALUES
            (10, 1, 'Talhao A', 50.0, 'Soja / Milho Safrinha', -22.7, -55.5, 'coords');
        """
    )
    connection.commit()
    connection.close()
    return {
        "users": [
            (1, "Admin Demo", "admin@orion.com", "admin-hash", "admin"),
            (2, "Produtor", "produtor@example.com", "user-hash", "Produtor Rural"),
        ],
        "farms": [
            (1, "Fazenda Demo", "Dourados", 100.5, -22.7, -55.5),
            (2, "Fazenda Privada", "Ponta Pora", 55.25, -22.6, -55.7),
        ],
        "talhoes": [
            (10, 1, "Talhao A", 50.0, "Soja / Milho Safrinha", -22.7, -55.5, "coords"),
        ],
    }


def _run_upgrade(database_path: Path) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = f"sqlite:///{database_path}"
    return subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _snapshot_farms(path: Path) -> dict:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row

    table_info = [
        {
            "name": row["name"],
            "type": row["type"].upper(),
            "nullable": row["notnull"] == 0,
            "default": row["dflt_value"],
            "pk": row["pk"],
        }
        for row in connection.execute("PRAGMA table_info(farms)")
    ]

    indexes = []
    for index in connection.execute("PRAGMA index_list(farms)"):
        index_name = index[1]
        columns = tuple(
            item[2]
            for item in connection.execute(
                f'PRAGMA index_info("{index_name.replace(chr(34), chr(34) * 2)}")'
            )
        )
        indexes.append((index_name, bool(index[2]), columns))

    foreign_keys = sorted(
        (
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
        )
        for row in connection.execute("PRAGMA foreign_key_list(farms)")
    )
    primary_key = tuple(row["name"] for row in table_info if row["pk"])

    result = {
        "table_info": table_info,
        "primary_key": primary_key,
        "indexes": sorted(indexes),
        "foreign_keys": foreign_keys,
    }
    connection.close()
    return result


def _rows(path: Path, table: str) -> list[tuple]:
    connection = sqlite3.connect(path)
    result = connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
    connection.close()
    return result


def test_schema_parity_new_vs_legacy_and_idempotency(tmp_path):
    """New metadata and a real legacy Alembic upgrade have the same farms schema."""
    new_path = tmp_path / "new.sqlite"
    legacy_path = tmp_path / "legacy.sqlite"

    new_engine = create_engine(f"sqlite:///{new_path}")
    Base.metadata.create_all(bind=new_engine)
    new_engine.dispose()

    expected = _create_legacy_database(legacy_path)

    first = _run_upgrade(legacy_path)
    assert first.returncode == 0, first.stderr

    new_schema = _snapshot_farms(new_path)
    legacy_schema = _snapshot_farms(legacy_path)
    assert legacy_schema == new_schema

    connection = sqlite3.connect(legacy_path)
    assert connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchall() == [("0002_canonical_location",)]
    connection.close()

    # A second upgrade must see the recorded revision and do nothing.
    second = _run_upgrade(legacy_path)
    assert second.returncode == 0, second.stderr
    connection = sqlite3.connect(legacy_path)
    assert connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchall() == [("0002_canonical_location",)]
    connection.close()

    # Existing records and the talhoes relationship survived the table rebuild.
    connection = sqlite3.connect(legacy_path)
    assert connection.execute(
        "SELECT id, name, email, hashed_password, role FROM users ORDER BY id"
    ).fetchall() == expected["users"]
    assert connection.execute(
        """
        SELECT id, name, city, total_area, latitude, longitude
          FROM farms ORDER BY id
        """
    ).fetchall() == expected["farms"]
    assert connection.execute(
        """
        SELECT id, farm_id, name, area, crop, latitude, longitude, kml_coordinates
          FROM talhoes ORDER BY id
        """
    ).fetchall() == expected["talhoes"]
    assert connection.execute(
        "SELECT owner_id, is_shared FROM farms ORDER BY id"
    ).fetchall() == [(1, 1), (1, 0)]

    # Both FK definitions are physically present after the migration.
    assert {
        (row[2], row[3], row[4])
        for row in connection.execute("PRAGMA foreign_key_list(farms)")
    } == {("users", "owner_id", "id")}
    assert {
        (row[2], row[3], row[4])
        for row in connection.execute("PRAGMA foreign_key_list(talhoes)")
    } == {("farms", "farm_id", "id")}

    # Turn enforcement on for this raw diagnostic connection and prove that
    # both physical constraints reject invalid references.
    connection.execute("PRAGMA foreign_keys=ON")
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """
            INSERT INTO farms
                (id, name, city, total_area, owner_id, is_shared)
            VALUES (99, 'Invalid', 'Cidade', 1, 999, 0)
            """
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO talhoes (id, farm_id, name, area) VALUES (99, 999, 'Invalid', 1)"
        )
    connection.rollback()
    connection.close()


def test_application_sqlite_connections_enable_foreign_keys():
    """Normal SQLAlchemy application connections enforce SQLite FKs."""
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_application_engine_rejects_invalid_owner_reference(client):
    """The application engine does not merely declare the FK; it enforces it."""
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO farms
                        (name, city, total_area, owner_id, is_shared)
                    VALUES ('Invalid', 'Cidade', 1, 999999, 0)
                    """
                )
            )
