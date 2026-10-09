"""Single bounded notification worker. Sampling never waits on a remote transport."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import threading
import time

from .incidents import atomic_write, incident_message, read_store
from .notifications import Notifier, NotificationResult
from .safe_read import read_text


def read_json(path: Path, default):
    try:
        return json.loads(read_text(path, max_bytes=8 * 1024 * 1024))
    except FileNotFoundError:
        return default


def destination_id(channel: str, value: str = '') -> str:
    return channel + ':' + hashlib.sha256(value.encode()).hexdigest()[:16]


def merge_delivery_summary(state: dict, state_path: Path) -> dict:
    """Present the new worker's outcomes without letting it write the sampler's state.json."""
    ledger = read_json(state_path / 'delivery.json', None)
    if ledger is None:
        return state
    latest = {}
    for event in ledger.get('events', {}).values():
        for key, row in event.get('destinations', {}).items():
            if row.get('attempted_at', 0) >= latest.get(key, {}).get('attempted_at', 0):
                latest[key] = row
    state = dict(state)
    errors = {}
    for row in latest.values():
        if row['status'] in ('retry', 'failed', 'unknown'):
            errors[row['channel']] = '일부 전달 실패 또는 접수 여부 미확인 · 경보 상세에서 기기별 상태를 확인하세요.'
    state['last_channel_errors'] = errors
    state['last_alert'] = max((r.get('attempted_at', 0) for r in latest.values()), default=state.get('last_alert', 0))
    mail = [r for r in latest.values() if r['channel'] == 'gmail']
    state['last_email_status'] = max((r.get('attempted_at', 0) for r in mail), default=state.get('last_email_status', 0))
    state['last_email_success'] = max((r.get('finished_at', 0) for r in mail if r['status'] == 'accepted'), default=state.get('last_email_success', 0))
    scheduled = read_json(state_path / 'scheduled-delivery.json', {})
    if scheduled.get('slot'):
        state['last_email_hour_slot'] = scheduled['slot']
    return state


class Delivery:
    def __init__(self, settings, shared: Path, *, clock=time.time):
        self.settings = settings
        self.shared = shared
        self.clock = clock
        self.path = settings.state_path / 'delivery.json'

    def destinations(self, notifier):
        from .webpush import load_json, SUBSCRIPTIONS_FILE, gone_endpoints
        destinations = {}
        if self.settings.gmail_enabled:
            recipients = [x.strip() for x in notifier.secrets.get('gmail_to', '').split(',') if x.strip()][:32]
            for index, recipient in enumerate(recipients):
                destinations[destination_id('gmail', recipient)] = {'channel': 'gmail', 'label': f'Gmail 수신처 {index + 1}', 'recipient': recipient}
        if self.settings.push_enabled:
            gone = gone_endpoints(self.settings.state_path)
            subscriptions = load_json(self.shared / SUBSCRIPTIONS_FILE, [])
            for index, subscription in enumerate(subscriptions[:10] if isinstance(subscriptions, list) else []):
                if not isinstance(subscription, dict) or not isinstance(subscription.get('endpoint'), str):
                    continue
                endpoint = subscription['endpoint']
                if endpoint not in gone:
                    destinations[destination_id('push', endpoint)] = {'channel': 'push', 'label': str(subscription.get('label') or f'푸시 기기 {index + 1}')[:40], 'subscription': subscription}
        for channel, enabled in (('windows_toast', self.settings.windows_toast_enabled),
                                 ('discord', self.settings.discord_enabled), ('webhook', self.settings.webhook_enabled)):
            if enabled:
                destinations[destination_id(channel)] = {'channel': channel, 'label': channel}
        return destinations

    def send(self, destination, row, notifier, origin):
        from . import webpush
        title, body, rich = incident_message(row, origin)
        channel = destination['channel']
        severity = 'recovery' if row['state'] == 'resolved' else row['severity']
        if channel == 'gmail':
            notifier.secrets = dict(notifier.secrets, gmail_to=destination['recipient'])
            return notifier._send_gmail(title, body, severity, rich)
        if channel == 'push':
            subscription = destination['subscription']
            vapid = webpush.load_json(self.shared / webpush.VAPID_FILE, {})
            payload = {'title': title[:120], 'body': ' '.join(body.split())[:280], 'severity': severity,
                       'tag': 'wrg-' + row['id'], 'incident_id': row['id'], 'revision': row['revision'],
                       'observed_at': row['observed_at'], 'url': '/#incident?id=' + row['id']}
            ok, detail, gone = webpush.send(webpush.validate_subscription(subscription), vapid,
                vapid.get('subject') or 'https://localhost', payload,
                urgency='high' if severity == 'critical' else 'normal', ttl=900)
            if gone:
                expired = webpush.gone_endpoints(self.settings.state_path)
                expired.add(subscription['endpoint'])
                # This file is owned by the daemon, not the root subscription registry.
                atomic_write(self.settings.state_path / webpush.EXPIRED_FILE, sorted(expired))
            return NotificationResult('push', ok, 'expired' if gone else detail, skipped=gone)
        if channel == 'windows_toast':
            return notifier._send_windows_toast(title, body)
        if channel == 'discord':
            return notifier._send_discord(title, body, severity)
        return notifier._send_webhook(title, body, severity)

    def tick(self, *, send=None, max_sends=4):
        from .daemon import in_quiet_hours
        now = self.clock()
        store = read_store(self.settings.state_path / 'incidents.json')
        if not store['updated_at'] or not 0 <= now - store['updated_at'] <= max(60, self.settings.interval_seconds * 3):
            return 0  # Stale state cannot generate a fresh alert or recovery claim.
        ledger = read_json(self.path, {'version': 1, 'events': {}})
        if not isinstance(ledger, dict) or not isinstance(ledger.get('events'), dict):
            raise ValueError('Invalid delivery state')
        records = ledger['events']
        if ledger.get('updated_at', 0) > now:
            for entry in records.values():
                entry['next_reminder_at'] = now + self.settings.reminder_cooldown_seconds
                for result in entry.get('destinations', {}).values():
                    result['next_at'] = min(result.get('next_at', now), now + 60)
        current_ids = {r['id'] for r in store['incidents']}
        records = {k: v for k, v in records.items() if k in current_ids}
        ledger['events'] = records
        notifier = Notifier(self.settings, self.shared)
        destinations = self.destinations(notifier)
        decisions = read_json(self.shared / 'incident-decisions.json', {})
        legacy_snooze = read_json(self.shared / 'alert-snooze.json', {}).get('until', 0)
        origin = read_json(self.shared / 'dashboard.json', {}).get('origin', '')
        count = 0
        # Critical/new incidents take precedence over ordinary reminders.
        ordered = sorted(store['incidents'], key=lambda r: (r['severity'] == 'critical', r['opened_at']), reverse=True)
        for row in ordered:
            if row['state'] not in ('active', 'resolved'):
                continue
            if row['state'] == 'resolved' and not self.settings.recovery_notifications:
                continue
            decision = decisions.get(row['id'], {})
            deferred = (decision.get('choice') == 'defer' and decision.get('until', 0) > now
                        and decision.get('at', 0) <= now and decision.get('revision') == row['revision'])
            entry = records.get(row['id'])
            if row['state'] == 'resolved' and not entry:
                continue  # Do not announce a historical recovery to a newly registered recipient.
            changed = not entry or entry.get('revision') != row['revision']
            interval = self.settings.critical_reminder_seconds if row['severity'] == 'critical' else self.settings.reminder_cooldown_seconds
            quiet = row['severity'] == 'warning' and in_quiet_hours(now, self.settings.alert_quiet_hours)
            repeat = bool(entry and now >= entry.get('next_reminder_at', 0) and row['state'] == 'active'
                          and not deferred and not quiet and legacy_snooze <= now)
            if changed or repeat:
                recovery_keys = list(entry.get('destinations', {})) if entry and row['state'] == 'resolved' else []
                entry = {'revision': row['revision'], 'next_reminder_at': now + interval,
                         'state': row['state'], 'destinations': {}, 'recovery_keys': recovery_keys}
                records[row['id']] = entry
            for key, destination in destinations.items():
                if row['state'] == 'resolved' and key not in entry.get('recovery_keys', []):
                    continue
                if key not in entry['destinations'] and len(entry['destinations']) >= 48:
                    oldest = min(entry['destinations'], key=lambda k: entry['destinations'][k].get('attempted_at', 0))
                    entry['destinations'].pop(oldest)
                delivery = entry['destinations'].setdefault(key, {'channel': destination['channel'],
                    'label': destination['label'], 'status': 'pending', 'attempts': 0, 'next_at': now})
                if delivery['status'] == 'sending':
                    delivery.update(status='unknown', detail='이전 전송이 중단됐습니다. 접수 여부를 확인할 수 없습니다.')
                if (delivery['status'] not in ('pending', 'retry') or delivery['next_at'] > now
                        or count >= max_sends or (deferred and not changed)):
                    continue
                delivery.update(status='sending', attempted_at=now, attempts=delivery['attempts'] + 1)
                ledger['updated_at'] = now
                self._save(ledger)  # Persist attempt before transport; restart cannot silently duplicate mail.
                try:
                    result = (send or self.send)(destination, row, notifier, origin)
                except Exception as exc:
                    result = NotificationResult(destination['channel'], False, type(exc).__name__)
                ambiguous = any(word in result.detail for word in ('Timeout', 'timed out', 'Disconnected'))
                status = ('accepted' if result.sent else 'skipped' if result.skipped else 'unknown' if ambiguous
                          else 'failed' if delivery['attempts'] >= 3 else 'retry')
                delivery.update(status=status, detail=('전송 서버에 접수됐습니다. 실제 수신 여부는 별도입니다.' if result.sent else
                    '구독 만료 또는 발송 조건 미충족' if result.skipped else
                    '접수 여부 미확인. 중복 발송을 피하기 위해 자동 재전송하지 않습니다.' if ambiguous else '전송에 실패했습니다.'),
                    finished_at=self.clock(), next_at=now + (60, 300, 900)[min(2, delivery['attempts'] - 1)])
                self._save(ledger)
                count += 1
        ledger['updated_at'] = self.clock()
        self._save(ledger)
        return count

    def reports(self):
        """Independent schedule; a report never advances an incident's reminder clock."""
        from .daemon import email_heartbeat_due, email_heartbeat_slot, send_weekly_report
        from datetime import datetime
        now = self.clock()
        send_weekly_report(self.settings, now)
        path = self.settings.state_path / 'scheduled-delivery.json'
        record = read_json(path, {})
        if not email_heartbeat_due(now, record.get('slot', ''), self.settings):
            return
        state = read_json(self.settings.state_path / 'state.json', {})
        observed = state.get('updated_at', 0)
        if not observed or not 0 <= now - observed <= max(60, self.settings.interval_seconds * 3):
            return
        record.update(slot=email_heartbeat_slot(now, self.settings), attempted_at=now, status='unknown')
        atomic_write(path, record)
        origin = read_json(self.shared / 'dashboard.json', {}).get('origin', '')
        text = ('WSL 정기 상태\n관측 시각: ' + datetime.fromtimestamp(observed).astimezone().isoformat(timespec='seconds')
                + '\n상태: ' + str(state.get('severity', 'unknown')) + '\n' + '\n'.join(state.get('reasons', []))
                + '\nTailscale 연결 후 경보별 원인과 영향을 확인하세요.\n' + str(origin) + '/#alerts')
        results = Notifier(self.settings, self.shared).send('WSL 정기 상태', text, 'report', channels={'gmail'})
        result = next((r for r in results if r.channel == 'gmail'), None)
        record.update(status='accepted' if result and result.sent else 'failed', finished_at=self.clock())
        atomic_write(path, record)

    def _save(self, ledger):
        atomic_write(self.path, ledger)


class DeliveryWorker:
    def __init__(self, settings, shared):
        self.engine = Delivery(settings, shared)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name='wrg-notifications', daemon=True)

    def start(self):
        self.thread.start()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.engine.tick()
                self.engine.reports()
            except Exception as exc:
                print(f'notification worker unavailable: {type(exc).__name__}', flush=True)
            self.stop_event.wait(2)

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=1)
