"""Live-request send authority and receipt-critical delivery orchestration."""

from __future__ import annotations

import asyncio
import re
import time

from onebot import OneBotError, OneBotRejected
from video_platforms import VideoError, VideoTarget, parse_source
from video_store import VideoStore


def send_request(text: str) -> VideoTarget | None:
    # A conservative live-request gate, not a general natural-language policy engine.
    if not isinstance(text, str):
        return None
    live = '\n'.join(line for line in text.splitlines()
                     if not line.lstrip().startswith(('>', '引用:', '引用：')))
    live = re.sub(r'【[^】]*】|“[^”]*”|"[^"\n]*"|`[^`]*`', '', live)
    intent = re.sub(r'https?://[^\s<>]+', '', live)
    if re.search(r'不要|别发|勿发|不需要|总结|讨论|解释|介绍|分析|能否|能不能|能解析|可以吗|能识别|[?？]|do not|don.t|metadata only', intent, re.I):
        return None
    if not re.search(r'解析|下载|发(?:送)?(?:一下|给我)?(?:这个)?视频|resolve|download|send\s+(?:this\s+)?video', intent, re.I):
        return None
    try:
        return parse_source(live)
    except VideoError:
        ids = re.findall(r'(?<![A-Za-z0-9])(?:BV[0-9A-Za-z]{10}|av[1-9][0-9]{0,19})(?![A-Za-z0-9])', live)
        if len(set(ids)) == 1 and 'http' not in live:
            return parse_source(ids[0])
        return None


async def send_video(service, lease, args: dict) -> dict:
    if not service.settings.send_enabled or service.sender is None:
        raise VideoError('UNAVAILABLE', 'Video sending is disabled or unavailable')
    supplied = parse_source(args['source'], args.get('part'))
    bound = lease.send_source
    if bound is None:
        return {'status': 'confirmation_required', 'message': '请明确说“解析并发送”并附一个视频链接。'}
    if (supplied.platform != bound.platform or supplied.part != bound.part
            or ((supplied.short or bound.short) and supplied.url != bound.url)
            or (not (supplied.short or bound.short) and supplied.content_id != bound.content_id)):
        raise VideoError('INVALID_SOURCE', 'Send source must match the current user request')
    deadline = min(time.monotonic() + min(90, service.settings.send_timeout), lease.deadline - 10)
    if deadline - time.monotonic() < 5:
        raise VideoError('TIMEOUT', 'Insufficient turn time to send video')
    artifact = None
    sending = claimed = False
    try:
        async with asyncio.timeout_at(deadline):
            async with asyncio.timeout(min(20, max(0.01, deadline - time.monotonic()))):
                metadata = await service.backend.resolve(bound.url, None if bound.short else bound.part)
            if metadata.get('status') != 'ok':
                return metadata
            canonical = parse_source(metadata['canonical_url'], metadata['selected_part'])
            identity = f'{canonical.platform}:{canonical.content_id}:{canonical.part}'
            if service.store is None:
                service.store = VideoStore(service.settings.delivery_database)
            prior = service.store.claim(lease.conversation, lease.message_id, identity)
            if prior is not None:
                return {'status': prior['status'], 'duplicate': True, 'message_id': prior['receipt'],
                        'message': 'Previous outcome retained; unknown means it may already have sent. Do not retry.'}
            claimed = True
            artifact = await service.materializer.materialize(bound.url, bound.part, metadata,
                                                             min(deadline - 5, time.monotonic() + 55))
            service.authorize(lease.nonce, lease.session)
            uri = service.materializer.uri(artifact)
            service.store.set(lease.conversation, lease.message_id, 'sending')
            sending = True
            try:
                receipt = await service.sender(lease.conversation, uri, min(25, max(0.01, deadline - time.monotonic())))
                message_id = receipt.get('message_id')
                if type(message_id) is not int or message_id <= 0:
                    raise OneBotError('video receipt missing')
            except OneBotRejected:
                service.store.set(lease.conversation, lease.message_id, 'rejected')
                sending = False
                return {'status': 'rejected', 'message': 'QQ rejected the video message'}
            service.store.set(lease.conversation, lease.message_id, 'sent', message_id)
            sending = False
            return {'status': 'sent', 'message_id': message_id, 'video': artifact.metadata,
                    'message': 'OneBot accepted the video. Client playback has not been verified.'}
    except asyncio.CancelledError:
        if claimed and service.store is not None:
            service.store.set(lease.conversation, lease.message_id, 'unknown' if sending else 'acquisition_failed')
        raise
    except (TimeoutError, OneBotError, OSError, ValueError, VideoError) as exc:
        if claimed and service.store is not None:
            service.store.set(lease.conversation, lease.message_id, 'unknown' if sending else 'acquisition_failed')
        if sending:
            return {'status': 'unknown', 'message': 'Video may have sent; do not automatically retry.'}
        if isinstance(exc, VideoError):
            raise
        raise VideoError('TIMEOUT' if isinstance(exc, TimeoutError) else 'UNAVAILABLE',
                         'Video acquisition failed or timed out') from exc
    finally:
        if artifact is not None:
            service.materializer.retain(artifact)
