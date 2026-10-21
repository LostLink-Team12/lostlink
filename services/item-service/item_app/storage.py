"""Immutable S3 sealing: conditional source read, bounded body, create-only destination."""

import hashlib
from datetime import UTC, datetime

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


class StorageError(Exception):
    def __init__(self, code, *, retryable=False):
        self.code, self.retryable = code, retryable


def s3_client(settings):
    options = {}
    if settings.aws_endpoint_url:
        options.update(
            endpoint_url=settings.aws_endpoint_url,
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
    return boto3.client(
        "s3",
        region_name=settings.aws_region,
        config=Config(
            signature_version="s3v4",
            connect_timeout=2,
            read_timeout=3,
            retries={"total_max_attempts": 2},
            s3={"addressing_style": "path"},
        ),
        **options,
    )


class PhotoStorage:
    def __init__(self, client):
        self.client = client

    def ready(self, bucket):
        self.client.head_bucket(Bucket=bucket)

    def presign(self, photo):
        seconds = int((photo.expires_at - datetime.now(UTC)).total_seconds())
        if seconds <= 0:
            raise StorageError("upload_expired")
        return self.client.generate_presigned_post(
            Bucket=photo.bucket,
            Key=photo.staging_key,
            Fields={"Content-Type": photo.content_type},
            Conditions=[
                {"Content-Type": photo.content_type},
                ["content-length-range", photo.size, photo.size],
            ],
            ExpiresIn=min(600, seconds),
        )

    def photo_url(self, photo):
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": photo.bucket, "Key": photo.sealed_key}, ExpiresIn=120
        )

    def _existing(self, photo):
        try:
            head = self.client.head_object(Bucket=photo.bucket, Key=photo.sealed_key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        metadata = head.get("Metadata", {})
        if (
            head["ContentLength"] != photo.size
            or head.get("ContentType") != photo.content_type
            or metadata.get("upload-id") != str(photo.id)
            or metadata.get("item-version") != str(photo.item_version)
            or len(metadata.get("sha256", "")) != 64
        ):
            raise StorageError("sealed_object_conflict")
        return head["ETag"]

    def seal(self, photo):
        try:
            # Crash recovery always reuses the first sealed snapshot, even if staging changed.
            existing = self._existing(photo)
            if existing:
                return existing
            head = self.client.head_object(Bucket=photo.bucket, Key=photo.staging_key)
            if head.get("LastModified") and head["LastModified"] > photo.expires_at:
                raise StorageError("upload_expired")
            if head["ContentLength"] != photo.size or head.get("ContentType") != photo.content_type:
                raise StorageError("invalid_photo_metadata")
            source = {"Bucket": photo.bucket, "Key": photo.staging_key, "IfMatch": head["ETag"]}
            if head.get("VersionId") and head["VersionId"] != "null":
                source["VersionId"] = head["VersionId"]
            result = self.client.get_object(**source)
            stream = result["Body"]
            try:
                body = stream.read(photo.size + 1)
            finally:
                stream.close()
            if len(body) != photo.size or result.get("ContentType") != photo.content_type:
                raise StorageError("invalid_photo_metadata")
            checksum = hashlib.sha256(body).hexdigest()
            try:
                response = self.client.put_object(
                    Bucket=photo.bucket,
                    Key=photo.sealed_key,
                    Body=body,
                    ContentType=photo.content_type,
                    IfNoneMatch="*",
                    Metadata={
                        "upload-id": str(photo.id),
                        "item-version": str(photo.item_version),
                        "sha256": checksum,
                    },
                )
                return response["ETag"]
            except ClientError as exc:
                if exc.response["Error"]["Code"] in {"PreconditionFailed", "412"}:
                    return self._existing(photo)
                raise
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in {"NoSuchKey", "404", "NotFound"}:
                if photo.expires_at <= datetime.now(UTC):
                    raise StorageError("upload_expired") from exc
                raise StorageError("photo_not_uploaded", retryable=True) from exc
            if code in {"PreconditionFailed", "412"}:
                raise StorageError("photo_changed_during_seal", retryable=True) from exc
            raise StorageError("storage_unavailable", retryable=True) from exc
