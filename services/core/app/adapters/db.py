from dataclasses import dataclass, field
from os import environ

from psycopg import Connection
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool

CORE_DB_USER = "switchboard_core"


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_port: int
    db_name: str
    db_password: str = field(repr=False)
    health_token: str = field(repr=False)
    slack_app_id: str | None = None
    slack_signing_secret: str | None = field(default=None, repr=False)
    execution_credentials: str | None = field(default=None, repr=False)

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            db_host=environ["SWITCHBOARD_DB_HOST"],
            db_port=int(environ.get("SWITCHBOARD_DB_PORT", "5432")),
            db_name=environ["SWITCHBOARD_DB_NAME"],
            db_password=environ["SWITCHBOARD_CORE_PASSWORD"],
            health_token=environ["SWITCHBOARD_HEALTH_TOKEN"],
            slack_app_id=environ.get("SWITCHBOARD_SLACK_APP_ID"),
            slack_signing_secret=environ.get("SWITCHBOARD_SLACK_SIGNING_SECRET"),
            execution_credentials=environ.get("SWITCHBOARD_EXECUTION_CREDENTIALS"),
        )

    def __post_init__(self) -> None:
        if (
            not all((self.db_host, self.db_name, self.db_password))
            or not 1 <= self.db_port <= 65535
        ):
            raise ValueError("Invalid core database configuration")
        if len(self.health_token) < 32 or not self.health_token.isascii():
            raise ValueError("Configure a separate, randomly generated health token")


def check_login(connection: Connection[tuple[object, ...]]) -> None:
    role = connection.execute(
        """
        SELECT session_user, current_user, rolsuper OR rolbypassrls
        FROM pg_catalog.pg_roles WHERE rolname = current_user
        """
    ).fetchone()
    if role != (CORE_DB_USER, CORE_DB_USER, False):
        raise RuntimeError("Core requires its restricted PostgreSQL login")


def create_pool(settings: Settings, *, max_size: int = 4) -> ConnectionPool[Connection]:
    return ConnectionPool(
        make_conninfo(
            host=settings.db_host,
            port=settings.db_port,
            dbname=settings.db_name,
            user=CORE_DB_USER,
            password=settings.db_password,
            connect_timeout=3,
            options="-c statement_timeout=5000 -c lock_timeout=3000",
        ),
        kwargs={"autocommit": True},
        configure=check_login,
        min_size=1,
        max_size=max_size,
        max_waiting=16,
        timeout=3,
        open=False,
    )
