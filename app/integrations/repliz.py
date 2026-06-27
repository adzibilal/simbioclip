import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

import httpx

from app.integrations.s3_uploader import S3Config, S3Uploader

logger = logging.getLogger("simbioclip.integrations.repliz")

REPLIZ_BASE = "https://api.repliz.com/public"


class ReplizError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class ReplizClient:
    def __init__(self, access_key: str, secret_key: str, timeout: float = 30.0):
        self.access_key = access_key
        self.secret_key = secret_key
        self.timeout = timeout

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{REPLIZ_BASE}{path}"
        auth = (self.access_key, self.secret_key)
        max_attempts = 4
        last_error = None

        for attempt in range(max_attempts):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.request(method, url, auth=auth, **kwargs)
                if resp.status_code == 429 and attempt < max_attempts - 1:
                    wait = 2 ** attempt
                    logger.warning(f"Repliz rate limited, retrying in {wait}s")
                    time.sleep(wait)
                    continue
                if resp.status_code >= 400:
                    try:
                        body = resp.json()
                        msg = body.get("message", resp.text)
                    except Exception:
                        msg = resp.text or f"HTTP {resp.status_code}"
                    raise ReplizError(msg, resp.status_code)
                if resp.status_code == 204 or not resp.content:
                    return {}
                return resp.json()
            except ReplizError:
                raise
            except Exception as e:
                last_error = e
                if attempt < max_attempts - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise ReplizError(str(last_error)) from e

        raise ReplizError(str(last_error))

    def list_accounts(self, page: int = 1, limit: int = 50) -> Dict[str, Any]:
        return self._request("GET", "/account", params={"page": page, "limit": limit})

    def account_count(self) -> Dict[str, Any]:
        return self._request("GET", "/account/count")

    def create_schedule(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/schedule", json=payload)


def build_s3_config_from_settings(settings) -> S3Config:
    return S3Config(
        endpoint=settings.aws_endpoint,
        access_key=settings.aws_access_key_id,
        secret_key=settings.aws_secret_access_key,
        bucket=settings.aws_bucket,
        region=settings.aws_region,
        use_path_style=settings.aws_use_path_style,
        folder_name=settings.aws_folder_name,
        public_base_url=settings.public_base_url,
    )


def upload_clip_to_s3(clip, job, settings) -> Tuple[Optional[str], Optional[str]]:
    s3_config = build_s3_config_from_settings(settings)
    if not s3_config.is_configured:
        return None, None

    uploader = S3Uploader(s3_config)

    job_id = job.id
    clip_id = clip.id
    video_key = f"jobs/{job_id}/clips/{clip_id}.mp4"

    video_url = None
    thumb_url = None

    if clip.file_path and os.path.isfile(clip.file_path):
        video_url = uploader.upload_file(clip.file_path, video_key, content_type="video/mp4")

    if clip.thumbnail_image_path and os.path.isfile(clip.thumbnail_image_path):
        thumb_key = f"jobs/{job_id}/clips/{clip_id}_thumb.jpg"
        thumb_url = uploader.upload_file(clip.thumbnail_image_path, thumb_key, content_type="image/jpeg")
    elif clip.thumbnail_url:
        from app.main import DATA_DIR
        local_thumb = os.path.join(DATA_DIR, "jobs", job_id, "thumbnails", f"{clip_id}.jpg")
        if os.path.isfile(local_thumb):
            thumb_key = f"jobs/{job_id}/clips/{clip_id}_thumb.jpg"
            thumb_url = uploader.upload_file(local_thumb, thumb_key, content_type="image/jpeg")

    return video_url, thumb_url


def build_public_media_urls(
    job_id: str,
    clip_id: str,
    api_token: str,
    public_base_url: str,
    has_custom_thumb: bool = False,
    s3_video_url: Optional[str] = None,
    s3_thumb_url: Optional[str] = None,
) -> Tuple[str, str]:
    if s3_video_url:
        video_url = s3_video_url
    else:
        base = public_base_url.rstrip("/")
        q = f"?token={api_token}"
        video_url = f"{base}/jobs/{job_id}/clips/{clip_id}{q}"

    if s3_thumb_url:
        thumb_url = s3_thumb_url
    elif has_custom_thumb:
        base = public_base_url.rstrip("/")
        thumb_url = f"{base}/api/jobs/{job_id}/clips/{clip_id}/custom-thumbnail-image?token={api_token}"
    else:
        base = public_base_url.rstrip("/")
        thumb_url = f"{base}/jobs/{job_id}/clips/{clip_id}/thumb?token={api_token}"

    return video_url, thumb_url
