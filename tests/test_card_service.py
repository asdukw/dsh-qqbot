from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bot'))

from card_fetch import RemoteCardContent  # noqa: E402
from card_service import CardService  # noqa: E402
from personas import PersonaStore  # noqa: E402

from cards import CardRegistry, normalise  # noqa: E402

CARD_BYTES = json.dumps({'spec': 'chara_card_v3', 'data': {
    'name': 'Library friend', 'description': 'A kind librarian', 'first_mes': 'Hello',
}}).encode()


class CardServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cards = CardRegistry(Path(self.tmp.name) / 'cards.db')
        self.default, _ = self.cards.install_payload(normalise({'name': 'Default'}))
        self.cards.use(self.default.id)
        self.personas = PersonaStore(Path(self.tmp.name) / 'cards.db', self.cards)
        self.admins = {1}
        self.bot = SimpleNamespace(cards=self.cards, personas=self.personas, is_admin=lambda user: user in self.admins)
        self.service = CardService(self.bot)
        self.event = SimpleNamespace(user_id=1, message_type='private', message_id=10)

    async def asyncTearDown(self):
        await self.service.close()
        self.personas.close()
        self.cards.close()
        self.tmp.cleanup()

    def lease(self, text='找一张图书管理员角色卡并下载', user=1, conversation='private:1', session='session-1'):
        event = SimpleNamespace(user_id=user, message_type='private', message_id=10)
        return self.service.open(event, conversation, session, text,
                                 self.personas.effective(conversation).token)

    async def discover(self, lease, count=1):
        async def fetch(url, **kwargs):
            return RemoteCardContent(CARD_BYTES, url, 'application/json')
        with patch('card_service.fetch_public_card_content', side_effect=fetch):
            return await self.service.call(lease.nonce, lease.session, 'sources', {
                'query': 'librarian', 'sources': [{'url': f'https://cards.example/{i}.json'} for i in range(count)],
            })

    async def install(self, lease, candidate):
        with patch('card_service.fetch_public_card_content', new_callable=AsyncMock,
                   return_value=RemoteCardContent(CARD_BYTES, 'https://cdn.example/card.json', 'application/json')):
            return await self.service.call(lease.nonce, lease.session, 'install', {'candidate_id': candidate})

    async def test_non_admin_forged_scope_unknown_session_and_revoked_lease(self):
        with self.assertRaises(ValueError):
            self.lease(user=2, conversation='private:2')
        with self.assertRaises(ValueError):
            self.lease(conversation='private:2')
        lease = self.lease()
        for nonce, session in (('made-up', lease.session), (lease.nonce, 'another')):
            with self.assertRaises(ValueError):
                await self.service.call(nonce, session, 'library', {'action': 'list'})
        self.service.revoke(lease)
        with self.assertRaises(ValueError):
            await self.service.call(lease.nonce, lease.session, 'library', {'action': 'list'})
        next_lease = self.lease()
        self.assertNotEqual(lease.nonce, next_lease.nonce)
        with self.assertRaises(ValueError):
            await self.service.call(lease.nonce, lease.session, 'library', {'action': 'list'})

    async def test_synthetic_poke_or_background_event_has_no_authority(self):
        event = SimpleNamespace(user_id=1, message_type='private', message_id=0)
        with self.assertRaises(ValueError):
            self.service.open(event, 'private:1', 's1', '下载角色卡',
                              self.personas.effective('private:1').token)
        self.assertEqual(self.service.leases, {})

    async def test_expiry_and_current_admin_are_rechecked(self):
        lease = self.lease()
        lease.expires = time.monotonic() - 1
        with self.assertRaises(ValueError):
            self.service.authorize(lease.nonce, lease.session)
        lease = self.lease()
        self.admins.clear()
        with self.assertRaises(ValueError):
            self.service.authorize(lease.nonce, lease.session)

    async def test_search_only_requires_confirmation_and_install_is_separate_from_use(self):
        lease = self.lease('搜索图书管理员角色卡')
        results = await self.discover(lease)
        candidate = results['candidates'][0]['candidate_id']
        result = await self.install(lease, candidate)
        self.assertEqual(result['status'], 'confirmation_required')
        self.assertEqual(self.cards.count(), 1)
        self.service.revoke(lease)
        lease = self.lease('下载第 1 张')
        result = await self.install(lease, candidate)
        self.assertEqual(result['status'], 'installed')
        self.assertFalse(result['switched'])
        self.assertEqual(self.cards.active().id, self.default.id)
        self.assertEqual(self.personas.effective('private:1').record.id, self.default.id)
        self.assertIsNone(lease.deferred)
        again = await self.install(lease, candidate)
        self.assertEqual(again, result)
        self.assertEqual(self.cards.count(), 2)
        later = self.lease('下载第 1 张')
        duplicate = await self.install(later, candidate)
        self.assertEqual(duplicate['status'], 'already_exists')
        self.assertEqual(duplicate['card_id'], result['card_id'])

    async def test_multiple_candidates_require_same_owner_number_selection(self):
        lease = self.lease()
        results = await self.discover(lease, 2)
        candidate = results['candidates'][1]['candidate_id']
        self.assertEqual((await self.install(lease, candidate))['status'], 'confirmation_required')
        selected = self.lease('下载第 2 张')
        wrong = results['candidates'][0]['candidate_id']
        with self.assertRaises(ValueError):
            await self.install(selected, wrong)
        self.assertEqual((await self.install(selected, candidate))['status'], 'installed')
        self.admins.add(2)
        other = self.lease(user=2, conversation='private:2')
        with self.assertRaises(ValueError):
            await self.install(other, candidate)
        self.service.candidates[candidate].expires = time.monotonic() - 1
        with self.assertRaises(ValueError):
            await self.install(selected, candidate)

    async def test_download_cannot_use_model_url_or_unregistered_candidate(self):
        lease = self.lease()
        for candidate in ('https://cards.example/card.json', '/srv/private.json', 'data:image/png;base64,QQ==', 'invented'):
            with self.assertRaises(ValueError):
                await self.install(lease, candidate)
        with self.assertRaises(ValueError):
            await self.service.call(lease.nonce, lease.session, 'library', {'action': 'list', 'user_id': 99})

    async def test_revoke_during_download_prevents_database_write(self):
        lease = self.lease()
        result = await self.discover(lease)
        async def delayed(url):
            self.service.revoke(lease)
            return RemoteCardContent(CARD_BYTES, url, 'application/json')
        with patch('card_service.fetch_public_card_content', side_effect=delayed):
            with self.assertRaises(ValueError):
                await self.service.install(lease, result['candidates'][0]['candidate_id'])
        self.assertEqual(self.cards.count(), 1)

    async def test_html_discovers_only_actual_download_link_not_guessed_api(self):
        lease = self.lease()
        html = b'<html><a href="/download/card.json">Download JSON</a><img src="avatar.png"></html>'
        with patch('card_service.fetch_public_card_content', new_callable=AsyncMock,
                   return_value=RemoteCardContent(html, 'https://cards.example/character/name', 'text/html')):
            results = await self.service.discover(lease, {'sources': [{'url': 'https://cards.example/character/name'}]})
        self.assertEqual(len(results['candidates']), 1)
        candidate = self.service.candidates[results['candidates'][0]['candidate_id']]
        self.assertEqual(candidate.download, 'https://cards.example/download/card.json')

    async def test_deferred_use_is_not_pending_until_explicit_commit(self):
        record, _ = self.cards.install_bytes(CARD_BYTES)
        lease = self.lease('切换到刚安装的角色卡')
        result = self.service.library(lease, {'action': 'use', 'query': str(record.id)})
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(self.personas.queued(), [])
        self.assertIsNone(self.personas.pending('private:1'))
        self.assertIsNotNone(lease.deferred)
        self.service.revoke(lease)
        job = self.service.commit(lease.deferred)
        self.assertEqual(job['phase'], 'queued')
        self.assertEqual(job['expected_token'], lease.persona_token)
        self.assertEqual(job['actor'], '1')
        self.assertEqual(self.cards.active().id, self.default.id)

    async def test_deferred_use_requires_request_and_rechecks_token_and_admin(self):
        record, _ = self.cards.install_bytes(CARD_BYTES)
        lease = self.lease('查一下角色卡')
        self.assertEqual(self.service.library(lease, {'action': 'use', 'query': str(record.id)})['status'], 'confirmation_required')
        self.assertIsNone(lease.deferred)
        lease = self.lease('切换角色')
        self.service.library(lease, {'action': 'use', 'query': str(record.id)})
        self.admins.clear()
        with self.assertRaises(ValueError):
            self.service.commit(lease.deferred)
        self.admins.add(1)
        job = self.personas.enqueue('private:1', record, actor='1')
        self.personas.bind(job['id'], 0)
        self.personas.update(job['id'], phase='complete')
        with self.assertRaises(ValueError):
            self.service.commit(lease.deferred)

    async def test_chinese_number_selection_and_negative_or_question_requests(self):
        lease = self.lease()
        results = await self.discover(lease, 2)
        candidate = results['candidates'][1]['candidate_id']
        for text in ('不要下载第二张', '如何下载第二张', '能不能下载第二张'):
            with self.subTest(text=text):
                request = self.lease(text)
                self.assertEqual((await self.install(request, candidate))['status'], 'confirmation_required')
        request = self.lease('把刚才第二张装上')
        self.assertEqual((await self.install(request, candidate))['status'], 'installed')

    async def test_old_single_candidate_needs_explicit_current_selection(self):
        lease = self.lease()
        result = await self.discover(lease)
        candidate = result['candidates'][0]['candidate_id']
        request = self.lease('下载另一个角色卡')
        self.assertEqual((await self.install(request, candidate))['status'], 'confirmation_required')
        self.assertEqual(self.cards.count(), 1)

    async def test_new_search_invalidates_old_ids_and_other_group_cannot_install(self):
        lease = self.lease()
        old = (await self.discover(lease))['candidates'][0]['candidate_id']
        newer = (await self.discover(lease))['candidates'][0]['candidate_id']
        self.assertNotEqual(old, newer)
        with self.assertRaises(ValueError):
            await self.install(lease, old)
        event = SimpleNamespace(user_id=1, message_type='group', group_id=42, message_id=11)
        group = self.service.open(event, 'group:42', 'group-session', '下载第 1 张',
                                  self.personas.effective('group:42').token)
        with self.assertRaises(ValueError):
            await self.install(group, newer)
        self.assertEqual(self.cards.count(), 1)

    async def test_expired_during_download_and_invalid_response_never_install(self):
        lease = self.lease()
        candidate = (await self.discover(lease))['candidates'][0]['candidate_id']
        async def expired(url):
            self.service.candidates[candidate].expires = time.monotonic() - 1
            return RemoteCardContent(CARD_BYTES, url, 'application/json')
        with patch('card_service.fetch_public_card_content', side_effect=expired):
            with self.assertRaises(ValueError):
                await self.service.install(lease, candidate)
        self.assertEqual(self.cards.count(), 1)

    async def test_shutdown_revokes_active_leases_and_closes_listener(self):
        await self.service.start()
        lease = self.lease()
        await self.service.close()
        self.assertEqual(self.service.leases, {})
        self.assertIsNone(self.service._server)
        with self.assertRaises(ValueError):
            self.service.authorize(lease.nonce, lease.session)

    async def test_bridge_real_http_rejects_missing_secret_and_accepts_active_lease(self):
        await self.service.start()
        lease = self.lease()
        async with httpx.AsyncClient(trust_env=False) as client:
            body = {'lease': lease.nonce, 'session': lease.session, 'operation': 'library', 'args': {'action': 'list'}}
            rejected = await client.post(self.service.url + '/card', json=body)
            self.assertEqual(rejected.status_code, 400)
            accepted = await client.post(self.service.url + '/card', json=body,
                                         headers={'authorization': 'Bearer ' + self.service.secret})
            self.assertEqual(accepted.status_code, 200, accepted.text)
            self.assertEqual(accepted.json()['cards'][0]['id'], self.default.id)
            self.service.revoke(lease)
            expired = await client.post(self.service.url + '/card', json=body,
                                        headers={'authorization': 'Bearer ' + self.service.secret})
            self.assertEqual(expired.status_code, 400)


if __name__ == '__main__':
    unittest.main()
