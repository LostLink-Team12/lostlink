"""Small protected examples demonstrating authorization and tenant-scoped storage."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from lostlink_common.auth import Principal, get_principal, require_roles
from lostlink_common.db import get_tenant_session
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ExampleRecord

router = APIRouter(prefix="/v1")
staff_access = require_roles("venue_admin", "venue_staff")
TenantSession = Annotated[Session, Depends(get_tenant_session, scope="function")]


class NewExample(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=100)


class ExampleView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    created_at: datetime


@router.get("/me", response_model=Principal, tags=["Identity"])
def me(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
    return principal


@router.post("/examples", response_model=ExampleView, status_code=201, tags=["Examples"])
def create_example(
    body: NewExample,
    principal: Annotated[Principal, Depends(staff_access)],
    session: TenantSession,
) -> ExampleRecord:
    record = ExampleRecord(tenant_id=principal.tenant_id, name=body.name)
    session.add(record)
    session.flush()
    return record


@router.get("/examples", response_model=list[ExampleView], tags=["Examples"])
def list_examples(
    _principal: Annotated[Principal, Depends(staff_access)],
    session: TenantSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> list[ExampleRecord]:
    # RLS filters even a query without a WHERE tenant_id clause.
    query = select(ExampleRecord).order_by(ExampleRecord.created_at, ExampleRecord.id)
    return list(session.scalars(query.limit(limit).offset(offset)))
