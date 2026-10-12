import hashlib
import io
import json
import ssl
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from deploy.hash_router import setup_connection as setup
from hash_router.__main__ import main as router_main


class ConnectionSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cert = self.root / 'server.pem'
        self.cert.write_text(ssl.DER_cert_to_PEM_cert(b'abc'), encoding='ascii')
        self.config = {'api_token': 'test-token-' + 'x' * 40,
                       'tls_certificate': str(self.cert), 'tls_key': 'test-only.key',
                       'control_port': 8443, 'public_host': '127.0.0.1'}
        (self.root / 'config.json').write_text(json.dumps(self.config), encoding='utf-8')
        (self.root / 'state.json').write_text('{"preserve": true}', encoding='utf-8')

    def initialize(self, **kwargs):
        with patch.object(setup.ssl, 'SSLContext'):
            return setup.initialize(self.root, self.root, '127.0.0.1', **kwargs)

    def test_fingerprint_matches_tls_der_not_pem_text(self):
        expected = hashlib.sha256(b'abc').hexdigest().upper()
        self.assertEqual(':'.join(expected[i:i+2] for i in range(0, 64, 2)),
                         setup.certificate_fingerprint(self.cert))

    def test_default_does_not_export_or_modify_configuration_and_state(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        bundle, config = self.initialize()
        self.assertEqual(self.config, config)
        self.assertEqual(self.config['api_token'], bundle['token'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_explicit_export_remains_compatible(self):
        bundle, _ = self.initialize(export_connection=True)
        exported = json.loads((self.root / 'sbp-connection.json').read_text())
        self.assertEqual(bundle, exported)
        self.assertEqual('hashroute-connection-v1', exported['format'])

    def test_default_preserves_old_export(self):
        path = self.root / 'sbp-connection.json'
        path.write_bytes(b'old export retained')
        self.initialize()
        self.assertEqual(b'old export retained', path.read_bytes())

    def test_cli_prints_actual_connection_and_fingerprint_without_export(self):
        bundle, config = self.initialize()
        output = io.StringIO()
        with patch.object(setup, 'initialize', return_value=(bundle, config)) as init:
            with patch('sys.argv', ['setup_connection', '--host', '127.0.0.1']):
                with redirect_stdout(output):
                    setup.main()
        self.assertFalse(init.call_args.kwargs['export_connection'])
        self.assertIn('HTTPS API port: 8443', output.getvalue())
        self.assertIn(setup.certificate_fingerprint(self.cert)[:47], output.getvalue())
        self.assertIn(setup.certificate_fingerprint(self.cert)[48:], output.getvalue())
        self.assertNotIn('Optional import file:', output.getvalue())

    def test_quiet_setup_does_not_print_credentials(self):
        bundle,config=self.initialize()
        output=io.StringIO()
        with patch.object(setup,'initialize',return_value=(bundle,config)):
            with patch('sys.argv',['setup_connection','--host','127.0.0.1','--quiet']):
                with redirect_stdout(output):setup.main()
        self.assertEqual('',output.getvalue())

    def test_summary_key_has_own_line_and_tty_highlighting(self):
        bundle,config=self.initialize()
        output=io.StringIO()
        with patch.object(output,'isatty',return_value=True),patch.dict(setup.os.environ,{},clear=True):
            with redirect_stdout(output):
                setup.show_connection(bundle,config,setup.certificate_fingerprint(self.cert))
        self.assertIn('\n\033[1;36m'+bundle['token']+'\033[0m\n',output.getvalue())
        self.assertIn('\033[1;32mHASH ROUTER',output.getvalue())

    def test_version_requires_no_config_or_listeners(self):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as result:
            router_main(['--version'])
        self.assertEqual(0, result.exception.code)
        self.assertEqual('Hash Router 82.0.19', output.getvalue().strip())
