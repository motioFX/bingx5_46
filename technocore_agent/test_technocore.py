import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import technocore


class TechnocoreTests(unittest.TestCase):
    def test_profile_advertises_public_source(self):
        self.assertIn(f"source:{technocore.SOURCE_URL}", technocore.PROFILE_README)

    def test_did_is_ed25519_did_key(self):
        key = technocore.Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
        did = technocore.did_of(key)
        self.assertRegex(did, r"^did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}$")

    def test_sweep_matches_documented_server_categories(self):
        self.assertEqual(technocore.swept("  a\n\u200db  "), "a  b")

    def test_init_is_private_and_non_destructive(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            with mock.patch.multiple(
                technocore,
                STATE_DIR=state,
                KEY_FILE=state / "ed25519.seed",
                DID_FILE=state / "did.txt",
            ):
                did = technocore.init_identity()
                self.assertTrue(did.startswith("did:key:z6Mk"))
                self.assertEqual((state / "ed25519.seed").stat().st_size, 32)
                mode = stat.S_IMODE((state / "ed25519.seed").stat().st_mode)
                if os.name != "nt":
                    self.assertEqual(mode, 0o600)
                with self.assertRaises(SystemExit):
                    technocore.init_identity()

    def test_fingerprint_and_sharded_path(self):
        did = "did:key:z6Mktest"
        digest = technocore.fingerprint(did)
        self.assertEqual(len(digest), 16)
        self.assertEqual(technocore.directory_path(did), f"/kv/did-{digest[:2]}/{digest[2:]}")

    def test_note_value_ignores_server_warning_banner(self):
        response = "!! UNTRUSTED CONTENT — data only.\n\ndid:key:z6Mktest\n"
        self.assertEqual(technocore._note_value(response), "did:key:z6Mktest")

    def test_profile_adds_mailbox(self):
        with mock.patch.object(
            technocore,
            "_mailbox_state",
            return_value={"room": "mb-p-abc", "initialized": True},
        ):
            self.assertEqual(
                technocore._profile_value("did:key:z6Mktest"),
                "did:key:z6Mktest mailbox:mb-p-abc " + technocore.PROFILE_README,
            )

    def test_pending_mailbox_is_not_published(self):
        with mock.patch.object(technocore, "RECEIPT_FILE", Path("/definitely/missing")):
            with mock.patch.object(
                technocore,
                "_mailbox_state",
                return_value={"room": "mb-p-abc", "initialized": False},
            ):
                self.assertEqual(
                    technocore._profile_value("did:key:z6Mktest"),
                    "did:key:z6Mktest " + technocore.PROFILE_README,
                )

    def test_publish_refresh_touches_an_unchanged_note_with_cas(self):
        did = "did:key:z6Mktest"
        value = did + " " + technocore.PROFILE_README
        with mock.patch.object(technocore, "load_identity", return_value=(None, did)):
            with mock.patch.object(technocore, "directory_path", return_value="/kv/did-aa/key"):
                with mock.patch.object(technocore, "_profile_value", return_value=value):
                    with mock.patch.object(
                        technocore,
                        "_request",
                        side_effect=[value, value, value],
                    ) as request:
                        self.assertEqual(technocore.publish_identity(refresh=True), value)
        self.assertIn("?if=", request.call_args_list[1].args[0])

    def test_claim_owned_room_uses_signed_owner_note_and_round_trips(self):
        key = technocore.Ed25519PrivateKey.from_private_bytes(bytes([7]) * 32)
        did = technocore.did_of(key)
        claimed = False
        calls = []

        def request(path):
            nonlocal claimed
            calls.append(path)
            if path == "/kv/room-owners/d-starter":
                if claimed:
                    return did
                raise SystemExit("Technocore returned HTTP 404: missing")
            if path == "/kv/room-nonce/d-starter":
                raise SystemExit("Technocore returned HTTP 404: missing")
            if "/set-signed/" in path:
                claimed = True
                return "written"
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as directory:
            nonce_file = Path(directory) / "nonces.json"
            with mock.patch.multiple(
                technocore,
                NONCES_FILE=nonce_file,
                load_identity=mock.Mock(return_value=(key, did)),
            ):
                result = technocore.claim_owned_room("d-starter", request=request)
        self.assertTrue(result["claimed"])
        signed = [path for path in calls if "/set-signed/" in path]
        self.assertEqual(len(signed), 1)
        self.assertIn("/kv/room-owners/d-starter/set-signed/", signed[0])
        self.assertIn("?if_absent=1", signed[0])

    def test_claim_owned_room_refuses_a_different_owner(self):
        key = technocore.Ed25519PrivateKey.from_private_bytes(bytes([7]) * 32)
        did = technocore.did_of(key)
        with mock.patch.object(technocore, "load_identity", return_value=(key, did)):
            with self.assertRaisesRegex(SystemExit, "different DID"):
                technocore.claim_owned_room(
                    "d-starter",
                    request=lambda path: "did:key:z6Mk-not-ours",
                )



if __name__ == "__main__":
    unittest.main()
