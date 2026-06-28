import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.integrations.repliz import ReplizClient, ReplizError, build_public_media_urls, build_s3_config_from_settings, upload_clip_to_s3
from app.models import Clip, Job, ReplizScheduleEntry
from app.settings_store import AppSettings, get_settings

logger = logging.getLogger("simbioclip.integrations.repliz_schedule")


def _validate_repliz_config(settings: AppSettings) -> None:
    if not settings.repliz_access_key or not settings.repliz_secret_key:
        raise ReplizError("Repliz credentials not configured. Set them in Settings.")
    s3_ok = bool(settings.aws_endpoint and settings.aws_access_key_id and settings.aws_bucket)
    public_ok = bool(settings.public_base_url.strip())
    if not s3_ok and not public_ok:
        raise ReplizError(
            "Either PUBLIC_BASE_URL or S3 storage (AWS_ENDPOINT + keys) must be configured."
        )


def _default_schedule_payload(
    *,
    title: str,
    description: str,
    post_type: str,
    video_url: str,
    thumb_url: str,
    account_id: str,
    schedule_at: datetime,
    tags: Optional[List[str]] = None,
    custom_thumb_enabled: bool = True,
) -> Dict[str, Any]:
    use_thumb = custom_thumb_enabled and bool(thumb_url)
    return {
        "title": title or "",
        "description": description or "",
        "topic": "",
        "type": post_type,
        "medias": [
            {
                "alt": "",
                "customThumbnail": use_thumb,
                "type": "video",
                "thumbnail": thumb_url if use_thumb else "",
                "url": video_url,
            }
        ],
        "meta": {"title": "", "description": "", "url": ""},
        "additionalInfo": {
            "isAiGenerated": True,
            "isDraft": False,
            "collaborators": [],
            "mentions": [],
            "music": {"id": "", "artist": "", "name": "", "thumbnail": ""},
            "products": [],
            "tags": tags or [],
        },
        "replies": [],
        "accountId": account_id,
        "scheduleAt": schedule_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }


def _sync_legacy_repliz_fields(clip: Clip) -> None:
    if clip.repliz_schedules:
        latest = clip.repliz_schedules[-1]
        clip.repliz_schedule_id = latest.schedule_id
        clip.repliz_scheduled_at = latest.scheduled_at
        clip.repliz_account_id = latest.account_id


def schedule_clip(
    clip: Clip,
    job: Job,
    *,
    account_id: str,
    schedule_at: datetime,
    post_type: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[List[str]] = None,
    account_name: Optional[str] = None,
    settings: Optional[AppSettings] = None,
    custom_thumb_enabled: bool = True,
) -> str:
    settings = settings or get_settings()
    _validate_repliz_config(settings)

    if not clip.file_path or not clip.download_url:
        raise ReplizError("Clip is not rendered yet.")

    if clip.repliz_is_scheduled_for(account_id):
        label = account_name or account_id
        raise ReplizError(f"Clip is already scheduled for account {label}.")

    s3_video_url = None
    s3_thumb_url = None
    if not settings.aws_access_key_id.startswith("...") and build_s3_config_from_settings(settings).is_configured:
        s3_video_url, s3_thumb_url = upload_clip_to_s3(clip, job, settings)

    video_url, thumb_url = build_public_media_urls(
        job.id,
        clip.id,
        settings.api_token,
        settings.public_base_url,
        has_custom_thumb=bool(clip.thumbnail_image_path),
        s3_video_url=s3_video_url,
        s3_thumb_url=s3_thumb_url,
    )

    resolved_post_type = post_type or settings.repliz_post_type or "video"
    payload = _default_schedule_payload(
        title=title or clip.title or "",
        description=description or clip.hook or clip.title or "",
        post_type=resolved_post_type,
        video_url=video_url,
        thumb_url=thumb_url,
        account_id=account_id,
        schedule_at=schedule_at,
        tags=tags,
        custom_thumb_enabled=custom_thumb_enabled,
    )

    client = ReplizClient(settings.repliz_access_key, settings.repliz_secret_key)
    result = client.create_schedule(payload)
    schedule_id = result.get("scheduleId")
    if not schedule_id:
        raise ReplizError("Repliz did not return a scheduleId.")

    clip.repliz_schedules.append(
        ReplizScheduleEntry(
            schedule_id=schedule_id,
            account_id=account_id,
            scheduled_at=schedule_at.isoformat(),
            account_name=account_name,
            post_type=resolved_post_type,
        )
    )
    _sync_legacy_repliz_fields(clip)
    job.save()
    logger.info(f"Scheduled clip {clip.id} on Repliz account {account_id}: {schedule_id}")
    return schedule_id


def schedule_clip_to_accounts(
    clip: Clip,
    job: Job,
    *,
    account_ids: List[str],
    schedule_at: datetime,
    post_type: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[List[str]] = None,
    account_names: Optional[Dict[str, str]] = None,
    settings: Optional[AppSettings] = None,
) -> Dict[str, Any]:
    if not account_ids:
        raise ReplizError("Select at least one Repliz account.")

    settings = settings or get_settings()
    account_names = account_names or {}
    scheduled: List[Dict[str, str]] = []
    errors: List[str] = []

    account_platforms: Dict[str, str] = {}
    try:
        client = ReplizClient(settings.repliz_access_key, settings.repliz_secret_key)
        accounts_data = client.list_accounts()
        for acc in accounts_data.get("docs", []):
            aid = str(acc.get("_id") or "")
            platform = (acc.get("type") or acc.get("platform") or "").lower()
            if aid:
                account_platforms[aid] = platform
    except Exception as e:
        logger.warning(f"Could not fetch account platforms for thumbnail filter: {e}")

    for account_id in account_ids:
        aid = str(account_id).strip()
        if not aid:
            continue
        platform = account_platforms.get(aid, "")
        is_youtube = platform == "youtube"
        try:
            schedule_id = schedule_clip(
                clip,
                job,
                account_id=aid,
                schedule_at=schedule_at,
                post_type=post_type,
                title=title,
                description=description,
                tags=tags,
                account_name=account_names.get(aid),
                settings=settings,
                custom_thumb_enabled=not is_youtube,
            )
            scheduled.append({"account_id": aid, "schedule_id": schedule_id})
        except ReplizError as e:
            errors.append(str(e))

    if not scheduled:
        raise ReplizError(errors[0] if len(errors) == 1 else "; ".join(errors))

    return {"scheduled": scheduled, "errors": errors}


def maybe_auto_schedule_clip(clip: Clip, job: Job, settings: Optional[AppSettings] = None) -> None:
    settings = settings or get_settings()
    if not settings.repliz_auto_schedule:
        return
    if not settings.repliz_default_account_id:
        logger.warning("Auto-schedule enabled but repliz_default_account_id is not set")
        return
    if clip.repliz_is_scheduled_for(settings.repliz_default_account_id):
        return
    if not clip.file_path:
        return

    if not clip.download_url and build_s3_config_from_settings(settings).is_configured:
        s3_video_url, _ = upload_clip_to_s3(clip, job, settings)
        if s3_video_url:
            clip.download_url = s3_video_url
            job.save()

    if not clip.download_url:
        logger.warning(f"Auto-schedule skipped: no download_url for clip {clip.id}")
        return

    offset = max(0, int(settings.repliz_schedule_offset_minutes))
    schedule_at = datetime.now(timezone.utc) + timedelta(minutes=offset)

    try:
        schedule_clip(
            clip,
            job,
            account_id=settings.repliz_default_account_id,
            schedule_at=schedule_at,
            post_type=settings.repliz_post_type,
            settings=settings,
        )
    except Exception as e:
        logger.warning(f"Auto-schedule failed for clip {clip.id}: {e}")
