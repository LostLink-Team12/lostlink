"""Read-only sealed Item photo access using the worker's workload identity."""

import boto3
from botocore.config import Config


def s3_client(settings):
    options = {
        "region_name": settings.aws_region,
        "config": Config(
            connect_timeout=2,
            read_timeout=5,
            retries={"total_max_attempts": 3},
            signature_version="s3v4",
        ),
    }
    if settings.aws_endpoint_url:
        options.update(
            endpoint_url=settings.aws_endpoint_url,
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
    return boto3.client("s3", **options)
