import tempfile
import unittest
from pathlib import Path
from deploy.hash_router.install_files import copy_configuration_once


class InstallTests(unittest.TestCase):
    def test_repeat_install_preserves_configuration_and_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'example';target=Path(tmp)/'etc'/'config.json'
            source.write_text('{"api_token":"placeholder"}',encoding='utf-8')
            self.assertTrue(copy_configuration_once(source,target))
            target.write_text('{"api_token":"user-secret"}',encoding='utf-8')
            before=target.read_bytes();source.write_text('{"api_token":"new-example"}',encoding='utf-8')
            self.assertFalse(copy_configuration_once(source,target))
            self.assertEqual(target.read_bytes(),before)
