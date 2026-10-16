"""
S3 storage service for Content Manager media uploads.

Uploads admin-provided image files to an S3 bucket and returns a public URL
that is stored in `bot_hotel_images_scraper.image_url`. The chat-side image
matcher works off the row title, so once the URL lands in the table the
existing pipeline surfaces it unchanged.

boto3 is synchronous; all network calls are dispatched through
asyncio.to_thread so the FastAPI event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import unquote
from uuid import uuid4

from config.settings import settings

logger = logging.getLogger(__name__)


# Authoritative content-type → extension map. The extension is derived from the
# validated content type, never from the (untrusted) original filename.
_MIME_TO_EXT: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


@dataclass
class S3UploadResult:
    # Value persisted in bot_hotel_images_scraper.image_url. For a private
    # bucket this is an s3://bucket/key pointer, NOT a viewable link — the
    # display URL is presigned on every read via resolve_display_url().
    stored_value: str
    key: str
    bucket: str


class S3StorageError(Exception):
    """Raised for configuration / upload failures the caller should surface."""


class S3StorageService:
    def __init__(self) -> None:
        self._client = None

    # ---- configuration helpers -------------------------------------------------

    @staticmethod
    def is_enabled() -> bool:
        return bool(str(getattr(settings, "s3_bucket", "") or "").strip())

    @staticmethod
    def allowed_mime_types() -> set[str]:
        raw = str(getattr(settings, "s3_allowed_mime_types", "") or "")
        return {part.strip().lower() for part in raw.split(",") if part.strip()}

    @staticmethod
    def max_upload_bytes() -> int:
        try:
            mb = float(getattr(settings, "s3_max_upload_mb", 10.0) or 10.0)
        except (TypeError, ValueError):
            mb = 10.0
        return int(max(0.1, mb) * 1024 * 1024)

    @staticmethod
    def _bucket() -> str:
        return str(getattr(settings, "s3_bucket", "") or "").strip()

    @staticmethod
    def _region() -> str:
        return str(getattr(settings, "s3_region", "") or "us-east-1").strip() or "us-east-1"

    @staticmethod
    def _uploads_prefix() -> str:
        return str(getattr(settings, "s3_uploads_prefix", "hotel-images") or "hotel-images").strip().strip("/")

    @classmethod
    def _direct_base_url(cls) -> str:
        return f"https://{cls._bucket()}.s3.{cls._region()}.amazonaws.com"

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import boto3  # type: ignore
        except Exception as exc:  # pragma: no cover - import guard
            raise S3StorageError(f"boto3 is not installed: {exc}") from exc

        access_key = str(getattr(settings, "s3_access_key_id", "") or "").strip()
        secret_key = str(getattr(settings, "s3_secret_access_key", "") or "").strip()
        kwargs: dict = {"region_name": self._region()}
        # Only pass explicit keys when both are set; otherwise fall back to the
        # standard AWS credential chain (env vars, shared config, instance role).
        if access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key
            kwargs["aws_secret_access_key"] = secret_key
        self._client = boto3.client("s3", **kwargs)
        return self._client

    # ---- key / url helpers -----------------------------------------------------

    @staticmethod
    def _slugify(value: str, *, fallback: str = "image") -> str:
        stem = re.sub(r"\.[^.]+$", "", str(value or "").strip())
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", stem).strip("-").lower()
        slug = slug[:60].strip("-")
        return slug or fallback

    @classmethod
    def _build_key(
        cls,
        *,
        hotel_code: str,
        property_id: int | None,
        original_filename: str,
        ext: str,
    ) -> str:
        hotel_part = cls._slugify(hotel_code, fallback="hotel")
        scope_part = str(property_id) if property_id else "shared"
        slug = cls._slugify(original_filename, fallback="image")
        unique = uuid4().hex[:12]
        prefix = cls._uploads_prefix()
        return f"{prefix}/{hotel_part}/{scope_part}/{unique}-{slug}.{ext}"

    @classmethod
    def is_managed_value(cls, stored_value: str) -> bool:
        """True when the stored image_url is one of our uploads (s3:// pointer).

        Paste-URL rows store an ordinary http(s) link and return False, so they
        flow through display/delete untouched.
        """
        return str(stored_value or "").strip().lower().startswith("s3://")

    @classmethod
    def owns_stored(cls, stored_value: str) -> bool:
        """True when the stored value is an object we uploaded to OUR bucket/prefix.

        Used by the delete path so we only ever remove objects we created.
        """
        if not cls.is_enabled():
            return False
        bucket, key = cls._parse_stored(stored_value)
        if not bucket or not key:
            return False
        return bucket == cls._bucket() and key.startswith(cls._uploads_prefix() + "/")

    @classmethod
    def _parse_stored(cls, stored_value: str) -> tuple[str, str]:
        """Return (bucket, key) for an s3://bucket/key pointer.

        Also tolerates a legacy https direct-S3 URL so older rows still resolve.
        Returns ("", "") for anything else (e.g. third-party paste URLs).
        """
        candidate = str(stored_value or "").strip()
        if not candidate:
            return "", ""
        if candidate.lower().startswith("s3://"):
            rest = candidate[5:]
            bucket, _, key = rest.partition("/")
            return bucket.strip(), unquote(key.strip())
        # Legacy: https://{bucket}.s3.{region}.amazonaws.com/{key}
        direct = cls._direct_base_url().rstrip("/")
        if direct and candidate.startswith(direct + "/"):
            return cls._bucket(), unquote(candidate[len(direct) + 1:])
        return "", ""

    def presign(self, stored_value: str) -> str:
        """Mint a time-limited GET URL for an s3:// pointer. Local HMAC sign,
        no network call. Non-managed values are returned unchanged."""
        if not self.is_managed_value(stored_value):
            return str(stored_value or "").strip()
        if not self.is_enabled():
            return ""
        bucket, key = self._parse_stored(stored_value)
        if not bucket or not key:
            return ""
        try:
            expires = int(getattr(settings, "s3_presign_expiry_seconds", 604800) or 604800)
        except (TypeError, ValueError):
            expires = 604800
        expires = max(60, min(expires, 604800))
        try:
            client = self._get_client()
            return client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=expires,
            )
        except Exception:
            logger.exception("S3 presign failed (bucket=%s key=%s)", bucket, key)
            return ""

    def resolve_display_url(self, stored_value: str) -> str:
        """Public choke point: convert a stored image_url into a browser-loadable
        URL. s3:// pointers are presigned; http(s) paste URLs pass through."""
        return self.presign(stored_value)

    # ---- public API ------------------------------------------------------------

    async def upload_image(
        self,
        *,
        file_bytes: bytes,
        content_type: str,
        hotel_code: str,
        property_id: int | None,
        original_filename: str,
    ) -> S3UploadResult:
        if not self.is_enabled():
            raise S3StorageError("S3 uploads are not configured (S3_BUCKET is empty)")

        normalized_type = str(content_type or "").split(";")[0].strip().lower()
        allowed = self.allowed_mime_types()
        if allowed and normalized_type not in allowed:
            raise S3StorageError(
                f"Unsupported file type '{normalized_type or 'unknown'}'. "
                f"Allowed: {', '.join(sorted(allowed))}"
            )

        if not file_bytes:
            raise S3StorageError("Uploaded file is empty")
        max_bytes = self.max_upload_bytes()
        if len(file_bytes) > max_bytes:
            raise S3StorageError(
                f"File is too large ({len(file_bytes) / (1024 * 1024):.1f} MB). "
                f"Max allowed is {max_bytes / (1024 * 1024):.0f} MB."
            )

        ext = _MIME_TO_EXT.get(normalized_type, "bin")
        key = self._build_key(
            hotel_code=hotel_code,
            property_id=property_id,
            original_filename=original_filename,
            ext=ext,
        )

        def _put() -> None:
            client = self._get_client()
            client.put_object(
                Bucket=self._bucket(),
                Key=key,
                Body=file_bytes,
                ContentType=normalized_type or "application/octet-stream",
                CacheControl="public, max-age=31536000, immutable",
            )

        try:
            await asyncio.to_thread(_put)
        except S3StorageError:
            raise
        except Exception as exc:
            logger.exception("S3 upload failed (bucket=%s key=%s)", self._bucket(), key)
            raise S3StorageError(f"S3 upload failed: {exc}") from exc

        stored_value = f"s3://{self._bucket()}/{key}"
        return S3UploadResult(stored_value=stored_value, key=key, bucket=self._bucket())

    async def upload_pdf(
        self,
        *,
        file_bytes: bytes,
        hotel_code: str,
        property_id: int | None,
        original_filename: str,
    ) -> S3UploadResult:
        """Upload a PDF document (a menu). Kept apart from upload_image so the
        image allowlist stays images-only; the key sits under the same prefix,
        so presign/delete treat it like any other upload of ours."""
        if not self.is_enabled():
            raise S3StorageError("S3 uploads are not configured (S3_BUCKET is empty)")
        if not file_bytes:
            raise S3StorageError("Uploaded file is empty")
        if not file_bytes.startswith(b"%PDF"):
            raise S3StorageError("Only PDF files can be uploaded as a menu.")
        max_bytes = self.max_upload_bytes()
        if len(file_bytes) > max_bytes:
            raise S3StorageError(
                f"File is too large ({len(file_bytes) / (1024 * 1024):.1f} MB). "
                f"Max allowed is {max_bytes / (1024 * 1024):.0f} MB."
            )
        key = self._build_key(
            hotel_code=hotel_code,
            property_id=property_id,
            original_filename=original_filename,
            ext="pdf",
        )

        def _put() -> None:
            client = self._get_client()
            client.put_object(
                Bucket=self._bucket(),
                Key=key,
                Body=file_bytes,
                ContentType="application/pdf",
                ContentDisposition="inline",
                CacheControl="public, max-age=31536000, immutable",
            )

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:
            logger.exception("S3 PDF upload failed (bucket=%s key=%s)", self._bucket(), key)
            raise S3StorageError(f"S3 upload failed: {exc}") from exc
        return S3UploadResult(stored_value=f"s3://{self._bucket()}/{key}", key=key, bucket=self._bucket())

    async def delete_stored(self, stored_value: str) -> bool:
        """Delete the S3 object backing this stored value. No-op for non-owned values."""
        if not self.owns_stored(stored_value):
            return False
        bucket, key = self._parse_stored(stored_value)
        if not bucket or not key:
            return False

        def _delete() -> None:
            client = self._get_client()
            client.delete_object(Bucket=bucket, Key=key)

        try:
            await asyncio.to_thread(_delete)
            return True
        except Exception:
            logger.exception("S3 delete failed (bucket=%s key=%s)", bucket, key)
            return False


s3_storage_service = S3StorageService()
