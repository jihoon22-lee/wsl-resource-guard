"""PID identity checks use synthetic processes; live signals target only our child."""
from dataclasses import replace
import errno
import os
import signal
import subprocess
import unittest
from unittest.mock import patch

from wsl_resource_guard import processes as p


def target(**changes):
    fields = dict(pid=424242, ppid=1, uid=os.getuid(), name='codex', state='S',
                  rss_kib=1, swap_kib=0, age_seconds=10, cwd='/',
                  cgroup='/user.slice/test.scope', command='codex', start_ticks=100)
    fields.update(changes)
    return p.ProcessInfo(**fields)


class VerifiedSignalTests(unittest.TestCase):
    def test_uses_pidfd_and_closes_it_for_term_and_kill(self):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(sig=sig):
                old = target()
                with (patch.object(p.os, 'pidfd_open', return_value=987) as opened,
                      patch.object(p, '_read_process', return_value=old),
                      patch.object(p.os, 'geteuid', return_value=old.uid),
                      patch.object(p.os, 'close') as close,
                      patch.object(p.signal, 'pidfd_send_signal') as send,
                      patch.object(p.os, 'kill') as numeric):
                    self.assertTrue(p.signal_verified_process(old, old.uid, sig))
                opened.assert_called_once_with(old.pid)
                send.assert_called_once_with(987, sig)
                close.assert_called_once_with(987)
                numeric.assert_not_called()

    def test_rejects_reused_uid_changed_or_protected_process(self):
        old = target()
        replacements = [replace(old, start_ticks=101), replace(old, uid=old.uid+1),
                        replace(old, cgroup='/system.slice/protected.service/child'),
                        replace(old, cgroup=''), replace(old, pid=1)]
        for current in replacements:
            with self.subTest(current=current):
                with (patch.object(p.os, 'pidfd_open', return_value=987),
                      patch.object(p, '_read_process', return_value=current),
                      patch.object(p.os, 'close') as close,
                      patch.object(p.signal, 'pidfd_send_signal') as send):
                    with self.assertRaises((PermissionError, ValueError)):
                        p.signal_verified_process(old, old.uid, signal.SIGTERM)
                close.assert_called_once_with(987)
                send.assert_not_called()

    def test_missing_token_self_and_init_are_never_signalled(self):
        for old in (target(start_ticks=None), target(pid=1), target(pid=os.getpid())):
            with self.subTest(old=old), patch.object(p.os, 'pidfd_open') as opened:
                with self.assertRaises((PermissionError, ValueError)):
                    p.signal_verified_process(old, old.uid, signal.SIGTERM)
                opened.assert_not_called()

    def test_exited_process_is_false_and_unsupported_kernel_never_falls_back(self):
        old = target()
        with patch.object(p.os, 'pidfd_open', side_effect=ProcessLookupError):
            self.assertFalse(p.signal_verified_process(old, old.uid, signal.SIGTERM))
        with (patch.object(p.os, 'pidfd_open', side_effect=OSError(errno.ENOSYS, 'fixture')),
              patch.object(p.os, 'kill') as numeric):
            with self.assertRaises(OSError):
                p.signal_verified_process(old, old.uid, signal.SIGTERM)
            numeric.assert_not_called()
        with (patch.object(p.os, 'pidfd_open', return_value=987),
              patch.object(p, '_read_process', return_value=None), patch.object(p.os, 'close') as close):
            self.assertFalse(p.signal_verified_process(old, old.uid, signal.SIGTERM))
            close.assert_called_once_with(987)

    def test_root_sends_as_owner_not_with_root_credentials(self):
        from types import SimpleNamespace
        old = target(uid=12345)
        with (patch.object(p.os, 'pidfd_open', return_value=987),
              patch.object(p, '_read_process', return_value=old), patch.object(p.os, 'close'),
              patch.object(p.os, 'geteuid', return_value=0),
              patch('pwd.getpwuid', return_value=SimpleNamespace(pw_gid=12346)),
              patch('subprocess.run', return_value=SimpleNamespace(returncode=0)) as child,
              patch.object(p.signal, 'pidfd_send_signal') as privileged):
            self.assertTrue(p.signal_verified_process(old, old.uid, signal.SIGTERM))
        privileged.assert_not_called()
        self.assertEqual(child.call_args.kwargs['user'],12345)
        self.assertEqual(child.call_args.kwargs['group'],12346)
        self.assertEqual(child.call_args.kwargs['extra_groups'],())
        self.assertEqual(child.call_args.kwargs['pass_fds'],(987,))

    def test_proc_snapshot_retains_identity_and_rejects_inconsistent_reads(self):
        current = p._read_process(os.getpid(), os.sysconf('SC_CLK_TCK'), 0)
        self.assertEqual(current.start_ticks, p.process_start_ticks(os.getpid()))
        with patch.object(p, 'process_start_ticks', side_effect=[100, 101]):
            self.assertIsNone(p._read_process(os.getpid(), os.sysconf('SC_CLK_TCK'), 0))

    @unittest.skipUnless(hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'), 'Linux pidfd required')
    def test_real_pidfd_only_terminates_disposable_child(self):
        child = subprocess.Popen(['/bin/sleep','30'])
        try:
            current = p._read_process(child.pid, os.sysconf('SC_CLK_TCK'), 0)
            self.assertIsNotNone(current)
            # CI may itself run under a service. The synthetic scope affects
            # only classification; the pidfd and identity read remain real.
            current.cgroup = '/fixture.scope'
            with patch.object(p, 'kill_block_reason', return_value=''):
                self.assertTrue(p.signal_verified_process(current, os.getuid(), signal.SIGTERM))
            self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)
        finally:
            if child.poll() is None: child.kill()
            child.wait()
