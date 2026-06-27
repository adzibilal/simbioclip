import logging
import os
from pathlib import Path
from typing import Optional, Tuple

import boto3
from botocore.config import Config

logger = logging.getLogger("simbioclip.integrations.s3_uploader")


class S3Config:
    def __init__(
        self,
        endpoint: str = "",
        access_key: str = "",
        secret_key: str = "",
        bucket: str = "",
        region: str = "us-east-1",
        use_path_style: bool = True,
        folder_name: str = "simbioclip",
        public_base_url: str = "",
    ):
        self.endpoint = endpoint.rstrip("/") if endpoint else ""
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket = bucket
        self.region = region
        self.use_path_style = use_path_style
        self.folder_name = folder_name.strip("/") if folder_name else ""
        self.public_base_url = public_base_url.rstrip("/") if public_base_url else ""

    @property
    def is_configured(self) -> bool:
        return bool(self.endpoint and self.access_key and self.secret_key and self.bucket)


class S3Uploader:
    def __init__(self, config: S3Config):
        self.config = config
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = boto3.client(
                "s3",
                endpoint_url=self.config.endpoint,
                aws_access_key_id=self.config.access_key,
                aws_secret_access_key=self.config.secret_key,
                region_name=self.config.region,
                config=Config(
                    s3={"addressing_style": "path" if self.config.use_path_style else "virtual"},
                    connect_timeout=10,
                    read_timeout=600,
                ),
            )
        return self._client

    def upload_file(self, local_path: str, remote_key: str, content_type: str = "video/mp4") -> Optional[str]:
        if not self.config.is_configured:
            logger.warning("S3 not configured, skipping upload")
            return None
        if not os.path.isfile(local_path):
            logger.error(f"File not found: {local_path}")
            return None

        key = f"{self.config.folder_name}/{remote_key}" if self.config.folder_name else remote_key
        extra_args = {
            "ContentType": content_type,
            "ACL": "public-read",
        }

        try:
            client = self._get_client()
            file_size_mb = os.path.getsize(local_path) / (1024 * 1024)
            logger.info(f"Uploading to S3: {key} ({file_size_mb:.1f} MB)")
            client.upload_file(local_path, self.config.bucket, key, ExtraArgs=extra_args)
            public_url = self._public_url(key)
            logger.info(f"S3 upload success: {public_url}")
            return public_url
        except Exception as e:
            logger.error(f"S3 upload failed for {key}: {e}")
            return None

    def upload_bytes(self, data: bytes, remote_key: str, content_type: str = "image/jpeg") -> Optional[str]:
        if not self.config.is_configured:
            return None
        key = f"{self.config.folder_name}/{remote_key}" if self.config.folder_name else remote_key
        try:
            client = self._get_client()
            client.put_object(
                Bucket=self.config.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                ACL="public-read",
            )
            url = self._public_url(key)
            logger.info(f"S3 bytes upload success: {url}")
            return url
        except Exception as e:
            logger.error(f"S3 bytes upload failed for {key}: {e}")
            return None

    def _public_url(self, key: str) -> str:
        base = self.config.endpoint.rstrip("/")
        return f"{base}/{self.config.bucket}/{key}"

    def test_connection(self) -> Tuple[bool, str]:
        try:
            client = self._get_client()
            resp = client.list_buckets()
            buckets = [b["Name"] for b in resp.get("Buckets", [])]
            exists = self.config.bucket in buckets
            if not exists:
                return False, f"Bucket '{self.config.bucket}' not found. Available: {', '.join(buckets) if buckets else 'none'}"
            return True, f"Connected. Bucket '{self.config.bucket}' OK"
        except Exception as e:
            return False, str(e)
