"""Run migrations with a separate owner credential, never the application role."""

from alembic import context
from app.models import DATABASE_SCHEMA, Base
from lostlink_common.db import identifier
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, pool, text


class MigrationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    migration_database_url: SecretStr
    database_app_role: str = "lostlink_app"


settings = MigrationSettings()
schema = identifier(DATABASE_SCHEMA)
role = identifier(settings.database_app_role)
url = settings.migration_database_url.get_secret_value()
if context.is_offline_mode():
    raise RuntimeError("Use online migrations to validate schema ownership and grants")


def include_name(name, type_, _parent_names):
    # Autogenerate must never propose dropping another service's tables.
    return name == DATABASE_SCHEMA if type_ == "schema" else True


engine = create_engine(url, poolclass=pool.NullPool)
try:
    with engine.connect() as connection:
        connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            version_table_schema=DATABASE_SCHEMA,
            include_schemas=True,
            include_name=include_name,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
            connection.execute(text(f"GRANT USAGE ON SCHEMA {schema} TO {role}"))
            # The runtime role may read migration state, but must never modify it.
            connection.execute(text(f"GRANT SELECT ON {schema}.alembic_version TO {role}"))
            for table in Base.metadata.sorted_tables:
                exists = connection.execute(
                    text("SELECT to_regclass(:qualified)"),
                    {"qualified": f"{DATABASE_SCHEMA}.{table.name}"},
                ).scalar()
                if exists is not None:
                    connection.execute(
                        text(
                            f"GRANT SELECT, INSERT, UPDATE, DELETE ON "
                            f"{schema}.{identifier(table.name)} TO {role}"
                        )
                    )
finally:
    engine.dispose()
