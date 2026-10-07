"""Concurrent fixed push operations share one lock and preserve subscriptions."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import pwd
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from wsl_resource_guard.service_control import Controller, ControlError, atomic_json
from wsl_resource_guard import webpush
from test_webpush import UA_PUBLIC,AUTH


def subscription(index):
    return {'endpoint':f'https://fcm.googleapis.com/fcm/send/{index}',
            'keys':{'p256dh':UA_PUBLIC,'auth':AUTH}}


class PushConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.shared=root/'shared'
        self.registry=root/'registry.json'
        atomic_json(self.registry,{'owner':pwd.getpwuid(os.getuid()).pw_name,
                                  'origin':'https://fixture.example','services':[]})
        self.controllers=[Controller(self.registry,Mock(),self.shared) for _ in range(4)]
        for c in self.controllers: c._owner_read=lambda *a,**k:[]

    def race(self,operations):
        barrier=threading.Barrier(len(operations))
        def start(op):
            barrier.wait(timeout=3)
            return op()
        with ThreadPoolExecutor(max_workers=len(operations)) as pool:
            jobs=[pool.submit(start,op) for op in operations]
            return [job.result(timeout=10) for job in jobs]

    def test_first_key_requests_return_one_persisted_key(self):
        generate=webpush.generate_vapid
        def slow_generate():
            time.sleep(.04)
            return generate()
        with patch.object(webpush,'generate_vapid',side_effect=slow_generate):
            results=self.race([c.push_key for c in self.controllers])
        saved=json.loads((self.shared/webpush.VAPID_FILE).read_text())
        self.assertEqual({r['public_key'] for r in results},{saved['public_key']})

    def test_simultaneous_subscribe_and_unsubscribe_preserve_other_updates(self):
        self.controllers[0].push_key()
        original=webpush.load_json
        def slow_load(path,default):
            value=original(path,default)
            if path.name==webpush.SUBSCRIPTIONS_FILE: time.sleep(.04)
            return value
        with patch.object(webpush,'load_json',side_effect=slow_load):
            self.race([lambda i=i,c=c:c.push_subscribe(subscription(i))
                       for i,c in enumerate(self.controllers)])
        saved=json.loads((self.shared/webpush.SUBSCRIPTIONS_FILE).read_text())
        self.assertEqual({s['endpoint'] for s in saved},{subscription(i)['endpoint'] for i in range(4)})
        with patch.object(webpush,'load_json',side_effect=slow_load):
            self.race([lambda:self.controllers[0].push_unsubscribe(subscription(0)['endpoint']),
                       lambda:self.controllers[1].push_subscribe(subscription(4))])
        saved=json.loads((self.shared/webpush.SUBSCRIPTIONS_FILE).read_text())
        self.assertEqual({s['endpoint'] for s in saved},{subscription(i)['endpoint'] for i in range(1,5)})

    def test_failure_releases_lock_and_does_not_regenerate_key(self):
        c=self.controllers[0]; key=c.push_key()['public_key']
        with patch.object(c,'write_shared',side_effect=OSError('fixture write failure')):
            with self.assertRaises(OSError): c.push_subscribe(subscription(0))
        self.controllers[1].push_subscribe(subscription(1))
        self.assertEqual(c.push_key()['public_key'],key)
        self.assertEqual(c.push_key()['subscriptions'],1)

    def test_status_checks_exact_local_subscription_without_returning_other_devices(self):
        c=self.controllers[0]
        c.push_subscribe(subscription(1))
        c.push_subscribe(subscription(2))
        self.assertEqual(c.push_status(subscription(1)),{'registered':True,'expired':False})
        other_key = dict(subscription(1),keys={'p256dh':webpush.generate_vapid()['public_key'],'auth':AUTH})
        self.assertFalse(c.push_status(other_key)['registered'])
        c.push_unsubscribe(subscription(1)['endpoint'])
        self.assertEqual(c.push_status(subscription(1)),{'registered':False,'expired':False})
        c._owner_read=lambda *a,**k:[subscription(2)['endpoint']]
        self.assertEqual(c.push_status(subscription(2)),{'registered':False,'expired':True})
