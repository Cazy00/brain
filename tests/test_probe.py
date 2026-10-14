import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("probe", ROOT / "deploy" / "probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class TokenFileTests(unittest.TestCase):
    """Prevents: a probe that cannot read the token file the minting script
    wrote, or that prints the secret while complaining about it."""

    def write(self, text):
        handle = tempfile.NamedTemporaryFile("w", delete=False, suffix=".tok")
        handle.write(text)
        handle.close()
        self.addCleanup(Path(handle.name).unlink)
        return Path(handle.name)

    def test_reads_the_cloudflare_api_response_shape(self):
        path = self.write(json.dumps({"result": {"client_id": "abc.access", "client_secret": "s3"}}))
        self.assertEqual(probe.read_token_file(path), ("abc.access", "s3"))

    def test_reads_flat_json(self):
        path = self.write(json.dumps({"client_id": "abc.access", "client_secret": "s3"}))
        self.assertEqual(probe.read_token_file(path), ("abc.access", "s3"))

    def test_reads_env_style_lines(self):
        path = self.write("# minted 2026-08-23\nCF_ACCESS_CLIENT_ID=abc.access\nCF_ACCESS_CLIENT_SECRET='s3'\n")
        self.assertEqual(probe.read_token_file(path), ("abc.access", "s3"))

    def test_an_unreadable_file_names_its_keys_and_never_its_values(self):
        path = self.write("UNRELATED=valuethatmustnotprint\n")
        with self.assertRaises(ValueError) as caught:
            probe.read_token_file(path)
        self.assertIn("unrelated", str(caught.exception))
        self.assertNotIn("valuethatmustnotprint", str(caught.exception))


class StreamParsingTests(unittest.TestCase):
    def test_an_sse_reply_yields_its_json(self):
        raw = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"b"},{"name":"a"}]}}\n\n'
        self.assertEqual(probe.tool_names(raw), ["a", "b"])

    def test_a_plain_json_reply_yields_its_tools(self):
        raw = '{"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"brain_search"}]}}'
        self.assertEqual(probe.tool_names(raw), ["brain_search"])


if __name__ == "__main__":
    unittest.main()
