"""Demonstration model; replace it with service-owned domain models when copying."""

from lostlink_common.db import TenantRecord
from sqlalchemy import MetaData, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DATABASE_SCHEMA = "service_template"
DATABASE_REVISION = "0001_template"


class Base(DeclarativeBase):
    metadata = MetaData(
        schema=DATABASE_SCHEMA, naming_convention={"ix": "ix_%(table_name)s_%(column_0_name)s"}
    )


class ExampleRecord(TenantRecord, Base):
    __tablename__ = "example_records"

    name: Mapped[str] = mapped_column(String(100), nullable=False)
