import argparse
from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import patch

from wsl_resource_guard import services


class ServiceClientTests(unittest.TestCase):
    def test_noninteractive_default_only_reads_status(self):
        with patch('sys.stdin.isatty', return_value=False), patch('builtins.input') as prompt:
            with patch.object(services, 'request_control', return_value={'services': [], 'origin': 'https://test:9443'}) as control:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(services.cmd_services(argparse.Namespace(action=None)), 0)
        prompt.assert_not_called()
        control.assert_called_once_with({'op': 'list'})

    def test_previous_opencode_command_remains_supported(self):
        with patch.object(services, 'request_control', return_value={'message': 'ok'}) as control:
            with redirect_stdout(io.StringIO()):
                services.cmd_services(argparse.Namespace(action='disable', service=None, confirm=False))
        control.assert_called_once_with({'op': 'action', 'id': 'opencode-web', 'action': 'disable', 'confirmed': False})

    def test_explicit_compose_registration_uses_key(self):
        with patch.object(services, 'request_control', return_value={'message': 'ok'}) as control:
            with redirect_stdout(io.StringIO()):
                services.cmd_services(argparse.Namespace(action='register', service='compose:demo'))
        control.assert_called_once_with({'op': 'register', 'key': 'compose:demo'})
