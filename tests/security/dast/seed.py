"""Seed only this disposable database; no workers, Cognito or real notifications."""

import os
from uuid import UUID

import psycopg

TENANTS = (str(UUID(int=1)), str(UUID(int=2)))
ITEMS = (str(UUID(int=11)), str(UUID(int=12)))
REPORTS = (str(UUID(int=21)), str(UUID(int=22)))


def seed():
    with psycopg.connect(os.environ["DAST_ADMIN_DSN"]) as connection:
        for index, suffix in enumerate(("a", "b")):
            connection.execute(
                "INSERT INTO venue.venues (id,name,slug,status,version) "
                "VALUES (%s,%s,%s,'active',1)",
                (TENANTS[index], f"DAST Venue {suffix}", f"dast-{suffix}"),
            )
            connection.execute(
                "INSERT INTO venue.memberships (id,tenant_id,user_sub,role,status,version) "
                "VALUES (%s,%s,%s,'venue_admin','active',1)",
                (str(UUID(int=31 + index)), TENANTS[index], f"dast-staff-{suffix}"),
            )
            connection.execute(
                "INSERT INTO item.items (id,tenant_id,title,description,category,found_at,state,"
                "item_version,version,created_by) "
                "VALUES (%s,%s,%s,%s,'test',now(),'available',1,1,%s)",
                (
                    ITEMS[index],
                    TENANTS[index],
                    f"DAST {suffix}",
                    "Synthetic isolation record",
                    f"dast-staff-{suffix}",
                ),
            )
            connection.execute(
                "INSERT INTO matching.reports (id,owner_id,description,has_photo,model_version,"
                "embedding,state,version) VALUES (%s,%s,'Synthetic isolation report',false,"
                "'fixture-not-inference',%s::vector,'open',1)",
                (REPORTS[index], f"dast-owner-{suffix}", "[1," + ",".join(["0"] * 511) + "]"),
            )


if __name__ == "__main__":
    seed()
