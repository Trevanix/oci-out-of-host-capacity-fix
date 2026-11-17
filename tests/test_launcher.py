"""Offline tests: every OCI call is mocked, nothing touches a real account.

Run:  python -m unittest discover tests
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import oci
from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main  # noqa: E402
import setup_wizard  # noqa: E402

ARM, MICRO = main.ARM_SHAPE, main.E2_MICRO_SHAPE


def inst(shape=ARM, ocpus=2, mem=12, state="RUNNING", id="i1", name="free-instance"):
    return SimpleNamespace(id=id, shape=shape, lifecycle_state=state, display_name=name,
                           availability_domain="xyz:EU-MADRID-1-AD-1", compartment_id="t",
                           shape_config=SimpleNamespace(ocpus=ocpus, memory_in_gbs=mem))


def capacity_error():
    return oci.exceptions.ServiceError(500, "InternalError", {}, "Out of host capacity.")


def settings(**overrides):
    s = main.load_settings({"DESKTOP_NOTIFY": "false", "REQUEST_WAIT_TIME_SECS": "0"})
    vars(s).update(overrides)
    return s


class InTempDir(unittest.TestCase):
    """The launcher writes INSTANCE_CREATED, logs, etc. into the current folder."""

    def setUp(self):
        self._old = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        patcher = mock.patch("time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        os.chdir(self._old)
        self._tmp.cleanup()


class SettingsTest(unittest.TestCase):
    def test_defaults_keep_original_sizes(self):
        s = main.load_settings({})
        self.assertEqual((s.shape, s.ocpus, s.memory_gb), (ARM, 2, 12))
        micro = main.load_settings({"OCI_COMPUTE_SHAPE": MICRO})
        self.assertEqual((micro.ocpus, micro.memory_gb), (1, 1))

    def test_ocpu_memory_and_paths_with_spaces(self):
        with tempfile.TemporaryDirectory() as d:
            env_file = Path(d) / "oci.env"
            env_file.write_text(setup_wizard.render_env({
                "OCI_CONFIG": "C:/Users/Jane Doe/My Folder/oci_config",
                "OPERATING_SYSTEM": "Canonical Ubuntu", "OCPUS": "4", "MEMORY_GB": "24",
                "OCT_FREE_AD": "AD-1, AD-2"}), encoding="utf-8")
            s = main.load_settings(dotenv_values(env_file))
        self.assertEqual(s.oci_config, "C:/Users/Jane Doe/My Folder/oci_config")
        self.assertEqual(s.operating_system, "Canonical Ubuntu")
        self.assertEqual((s.ocpus, s.memory_gb, s.ad_suffixes), (4, 24, ["AD-1", "AD-2"]))

    def test_bad_number_is_config_error(self):
        with self.assertRaises(main.ConfigError):
            main.load_settings({"OCPUS": "four"})


class FreeTierTest(unittest.TestCase):
    def check(self, shape=ARM, ocpus=4, mem=24, disk=50, instances=(), used=0, replacing=None):
        return main.free_tier_problems(shape, ocpus, mem, disk, list(instances), used, replacing)

    def test_full_a1_fits(self):
        self.assertEqual(self.check(), [])

    def test_existing_a1_counts(self):
        self.assertEqual(self.check(ocpus=2, mem=12, instances=[inst()]), [])
        problems = self.check(instances=[inst()])
        self.assertEqual(len(problems), 2)  # OCPUs and memory
        self.assertIn("OCPUs", problems[0])

    def test_terminated_and_resized_instance_not_counted(self):
        self.assertEqual(self.check(instances=[inst(state="TERMINATED")]), [])
        self.assertEqual(self.check(disk=0, instances=[inst(id="me")], replacing="me"), [])

    def test_memory_per_ocpu_ratio(self):
        self.assertTrue(any("per OCPU" in p for p in self.check(ocpus=4, mem=2)))

    def test_micro_limit_and_paid_shapes(self):
        self.assertEqual(self.check(MICRO, 1, 1, instances=[inst(MICRO)]), [])
        self.assertTrue(self.check(MICRO, 1, 1, instances=[inst(MICRO), inst(MICRO, id="i2")]))
        self.assertIn("not an Always Free shape", self.check("VM.Standard.E4.Flex")[0])

    def test_storage(self):
        self.assertEqual(self.check(disk=50, used=150), [])
        self.assertIn("Storage", self.check(disk=100, used=150)[0])
        self.assertIn("at least 50", self.check(disk=40)[0])

    def test_storage_used_sums_boot_and_block_volumes(self):
        vol = lambda gb, state="AVAILABLE": SimpleNamespace(size_in_gbs=gb, lifecycle_state=state)
        bs = mock.Mock()
        bs.list_volumes.return_value = SimpleNamespace(data=[vol(50), vol(100, "TERMINATED")])
        bs.list_boot_volumes.return_value = SimpleNamespace(data=[vol(47)])
        clients = SimpleNamespace(blockstorage=bs)
        self.assertEqual(main.storage_used_gb(clients, "t", ["AD-1", "AD-2"]), 50 + 47 * 2)


class RetryTest(InTempDir):
    def test_retryable(self):
        self.assertTrue(main.is_retryable({"status": 500, "code": "InternalError",
                                           "message": "Out of host capacity."}))
        self.assertTrue(main.is_retryable({"status": 429, "code": "TooManyRequests", "message": ""}))
        self.assertFalse(main.is_retryable({"status": 401, "code": "NotAuthenticated", "message": "x"}))

    def test_execute_retries_until_success(self):
        client = mock.Mock()
        client.get_thing.side_effect = [capacity_error(), capacity_error(), SimpleNamespace(data="ok")]
        self.assertEqual(main.execute_oci_command(client, "get_thing", 1), "ok")
        self.assertEqual(client.get_thing.call_count, 3)

    def test_execute_raises_on_real_error(self):
        client = mock.Mock()
        client.get_thing.side_effect = oci.exceptions.ServiceError(404, "NotAuthorizedOrNotFound", {}, "no")
        with self.assertRaises(Exception):
            main.execute_oci_command(client, "get_thing")


def subnet(id, public=True, ad=None, state="AVAILABLE"):
    return SimpleNamespace(id=id, prohibit_public_ip_on_vnic=not public, availability_domain=ad,
                           lifecycle_state=state)


class NetworkTest(InTempDir):
    def clients(self, subnets):
        net = mock.Mock()
        net.list_subnets.return_value = SimpleNamespace(data=subnets)
        return SimpleNamespace(network=net)

    def test_picks_public_subnet_in_right_ad(self):
        c = self.clients([subnet("private", public=False), subnet("other-ad", ad="AD-9"),
                          subnet("deleting", state="TERMINATING"), subnet("good")])
        self.assertEqual(main.find_subnet(settings(), c, "t", ["AD-1"]), "good")

    def test_configured_subnet_wins_and_none_found(self):
        self.assertEqual(main.find_subnet(settings(subnet_id="s1"), self.clients([]), "t", []), "s1")
        self.assertIsNone(main.find_subnet(settings(), self.clients([subnet("p", public=False)]), "t", []))

    def test_create_public_network(self):
        net = mock.Mock()
        vcn = SimpleNamespace(id="vcn", default_route_table_id="rt", default_security_list_id="sl")
        net.create_vcn.return_value = SimpleNamespace(data=vcn)
        net.create_internet_gateway.return_value = SimpleNamespace(data=SimpleNamespace(id="igw"))
        net.create_subnet.return_value = SimpleNamespace(data=SimpleNamespace(id="sub"))
        with mock.patch("oci.wait_until", side_effect=lambda c, r, *a: r):
            net.get_vcn.return_value = SimpleNamespace(data=vcn)
            net.get_subnet.return_value = SimpleNamespace(data=SimpleNamespace(id="sub"))
            self.assertEqual(main.create_public_network(SimpleNamespace(network=net), "t"), "sub")
        route = net.update_route_table.call_args[0][1].route_rules[0]
        self.assertEqual((route.destination, route.network_entity_id), ("0.0.0.0/0", "igw"))
        details = net.create_subnet.call_args[0][0]
        self.assertFalse(details.prohibit_public_ip_on_vnic)
        self.assertEqual(details.security_list_ids, ["sl"])


class FakeOci:
    """Mocked clients for a tenancy with one AD, no instances and no network."""

    def __init__(self, instances_seq, subnets=(), launch_effects=()):
        self.identity = mock.Mock()
        self.identity.list_availability_domains.return_value = SimpleNamespace(
            data=[SimpleNamespace(name="xyz:EU-MADRID-1-AD-1")])
        self.compute = mock.Mock()
        self.compute.list_instances.side_effect = [SimpleNamespace(data=x) for x in instances_seq]
        self.compute.list_images.return_value = SimpleNamespace(data=[])
        self.compute.launch_instance.side_effect = list(launch_effects)
        self.network = mock.Mock()
        self.network.list_subnets.return_value = SimpleNamespace(data=list(subnets))
        self.blockstorage = mock.Mock()
        self.blockstorage.list_volumes.return_value = SimpleNamespace(data=[])
        self.blockstorage.list_boot_volumes.return_value = SimpleNamespace(data=[])


class LaunchTest(InTempDir):
    def s(self, **kw):
        Path("key.pub").write_text("ssh-rsa AAAA test", encoding="utf-8")
        return settings(image_id="img", ssh_key_file="key.pub", **kw)

    def test_retries_capacity_then_announces(self):
        fake = FakeOci([[], [], [inst(ocpus=4, mem=24)]], subnets=[subnet("sub")],
                       launch_effects=[capacity_error(), capacity_error(), SimpleNamespace(status=200)])
        with mock.patch.object(main, "notify") as notify:
            main.launch_instance(self.s(ocpus=4, memory_gb=24), fake, "t")
        self.assertEqual(fake.compute.launch_instance.call_count, 3)
        details = fake.compute.launch_instance.call_args.kwargs["launch_instance_details"]
        self.assertEqual((details.shape_config.ocpus, details.shape_config.memory_in_gbs), (4, 24))
        self.assertEqual(details.create_vnic_details.subnet_id, "sub")
        self.assertIn("OCI instance created", notify.call_args[0][1])
        self.assertIn("Shape: VM.Standard.A1.Flex", Path("INSTANCE_CREATED").read_text())
        self.assertIn("and 3 attempts.", notify.call_args[0][2])

    def test_elapsed_summary(self):
        self.assertEqual(main.elapsed_summary(100, 791, now=100 + 13 * 3600 + 27 * 60 + 59),
                         "Got it after 13h 27m and 791 attempts.")
        self.assertEqual(main.elapsed_summary(0, 1, now=125), "Got it after 0h 02m and 1 attempt.")

    def test_existing_instance_stops_immediately(self):
        fake = FakeOci([[inst()]])
        with mock.patch.object(main, "notify"):
            main.launch_instance(self.s(), fake, "t")
        fake.compute.launch_instance.assert_not_called()

    def test_refuses_outside_free_tier(self):
        fake = FakeOci([[], [inst(state="STOPPED")]])  # stopped A1 still uses 2/12
        with self.assertRaises(main.ConfigError):
            main.launch_instance(self.s(ocpus=4, memory_gb=24), fake, "t")
        fake.compute.launch_instance.assert_not_called()

    def test_creates_network_when_missing(self):
        fake = FakeOci([[], [], [inst()]], launch_effects=[SimpleNamespace(status=200)])
        with mock.patch.object(main, "create_public_network", return_value="new-sub") as create, \
                mock.patch.object(main, "notify"):
            main.launch_instance(self.s(), fake, "t")
        create.assert_called_once()
        details = fake.compute.launch_instance.call_args.kwargs["launch_instance_details"]
        self.assertEqual(details.create_vnic_details.subnet_id, "new-sub")

    def test_no_network_and_creation_disabled(self):
        fake = FakeOci([[], []])
        with self.assertRaises(main.ConfigError):
            main.launch_instance(self.s(create_network=False), fake, "t")

    def test_bad_ad_suffix(self):
        with self.assertRaises(main.ConfigError):
            main.launch_instance(self.s(ad_suffixes=["AD-3"]), FakeOci([]), "t")

    def test_ssh_key_generated_when_missing(self):
        key = main.read_or_generate_ssh_public_key(Path("sub dir") / "new.pub")
        self.assertTrue(key.startswith("ssh-rsa "))
        self.assertTrue(Path("sub dir/new_private").read_text().startswith("-----BEGIN OPENSSH PRIVATE KEY"))


class ResizeTest(InTempDir):
    def test_resizes_with_retry(self):
        fake = FakeOci([[inst(), inst(MICRO, id="m")]])
        fake.compute.update_instance.side_effect = [capacity_error(), SimpleNamespace(data="done")]
        with mock.patch.object(main, "notify"):
            self.assertEqual(main.resize_instance(settings(), fake, "t"), "done")
        details = fake.compute.update_instance.call_args[0][1]
        self.assertEqual((details.shape_config.ocpus, details.shape_config.memory_in_gbs), (4, 24))

    def test_already_big_enough(self):
        fake = FakeOci([[inst(ocpus=4, mem=24)]])
        main.resize_instance(settings(), fake, "t")
        fake.compute.update_instance.assert_not_called()

    def test_refuses_when_other_a1_uses_quota(self):
        fake = FakeOci([[inst(), inst(id="other", name="second")]])
        with self.assertRaises(main.ConfigError):
            main.resize_instance(settings(), fake, "t")

    def test_ambiguous_instance(self):
        fake = FakeOci([[inst(name="a"), inst(id="b", name="b", ocpus=1, mem=6)]])
        with self.assertRaises(main.ConfigError):
            main.resize_instance(settings(), fake, "t")


class NotifyTest(InTempDir):
    def test_discord_and_telegram(self):
        s = settings(discord_webhook="https://discord.example/hook", telegram_token="T",
                     telegram_user_id="42")
        with mock.patch("requests.post") as post:
            main.notify(s, "Title", "Body")
        urls = [c.args[0] for c in post.call_args_list]
        self.assertEqual(urls, ["https://discord.example/hook", "https://api.telegram.org/botT/sendMessage"])
        self.assertEqual(post.call_args.kwargs["data"]["chat_id"], "42")

    def test_desktop_only_when_enabled(self):
        with mock.patch.object(main, "desktop_notify") as desk:
            main.notify(settings(), "t", "b")
            desk.assert_not_called()
            main.notify(settings(desktop_notify=True), "t", "b", desktop=False)
            desk.assert_not_called()
            main.notify(settings(desktop_notify=True), "t", "b")
            desk.assert_called_once_with("t", "b")

    def test_desktop_linux_and_mac(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch("shutil.which", return_value="/usr/bin/notify-send"), \
                mock.patch("subprocess.run") as run:
            main.desktop_notify("T", "B")
        self.assertEqual(run.call_args[0][0][-2:], ["T", "B"])
        with mock.patch.object(sys, "platform", "darwin"), mock.patch("subprocess.run") as run:
            main.desktop_notify('T "x"', "B")
        self.assertIn('with title "T \\"x\\""', run.call_args[0][0][2])

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_desktop_windows_uses_topmost_messagebox(self):
        with mock.patch("ctypes.windll.user32.MessageBoxW") as box:
            main.desktop_notify("T", "B")
            for t in __import__("threading").enumerate():
                if t is not __import__("threading").current_thread():
                    t.join(1)
        flags = box.call_args[0][3]
        self.assertTrue(flags & 0x40000)  # MB_TOPMOST


class MainTest(InTempDir):
    def test_missing_config_writes_error_log(self):
        Path("oci.env").write_text('OCI_CONFIG="does not exist/oci_config"\n', encoding="utf-8")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OCI_CONFIG", None)
            self.assertEqual(main.main(["check"]), 2)
        self.assertTrue(Path("ERROR_IN_CONFIG.log").read_text())
        for logger in (main.log, main.logging.getLogger()):  # release the log files (Windows locks them)
            for h in logger.handlers[:]:
                h.close()
                logger.removeHandler(h)


class WizardTest(InTempDir):
    def test_api_key_works_with_oci_config(self):
        fp = setup_wizard.generate_api_key(Path("api key.pem"), Path("api key public.pem"))
        self.assertRegex(fp, r"^([0-9a-f]{2}:){15}[0-9a-f]{2}$")
        values = {"user": "ocid1.user.oc1..aaaa", "tenancy": "ocid1.tenancy.oc1..aaaa",
                  "region": "eu-madrid-1", "fingerprint": fp}
        self.assertEqual(setup_wizard.validate_identity(values), [])
        Path("oci_config").write_text(setup_wizard.render_oci_config(values, Path("api key.pem").resolve()))
        config = oci.config.from_file("oci_config")  # also validates the format
        signer = oci.signer.Signer(config["tenancy"], config["user"], fp, config["key_file"])
        self.assertIsNotNone(signer)

    def test_fingerprint_matches_openssl_style(self):
        # Known pair: fingerprint is MD5 over the DER SubjectPublicKeyInfo
        from cryptography.hazmat.primitives import serialization
        import hashlib
        setup_wizard.generate_api_key(Path("k.pem"), Path("k.pub.pem"))
        pem = Path("k.pub.pem").read_bytes()
        der = serialization.load_pem_public_key(pem).public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.assertEqual(setup_wizard.fingerprint(pem).replace(":", ""), hashlib.md5(der).hexdigest())

    def test_parse_console_preview(self):
        preview = ("[DEFAULT]\nuser=ocid1.user.oc1..x\nfingerprint=aa:bb\n"
                   "tenancy=ocid1.tenancy.oc1..y\nregion=eu-madrid-1\nkey_file=<path>\n")
        self.assertEqual(setup_wizard.parse_config_preview(preview),
                         {"user": "ocid1.user.oc1..x", "fingerprint": "aa:bb",
                          "tenancy": "ocid1.tenancy.oc1..y", "region": "eu-madrid-1"})
        self.assertEqual(len(setup_wizard.validate_identity({"region": "Madrid"})), 4)

    def test_env_round_trip_with_tricky_values(self):
        values = {"A": 'say "hi"', "B": r"C:\new folder\x", "C": "", "D": "p@ss $word"}
        Path("e.env").write_text(setup_wizard.render_env(values), encoding="utf-8")
        self.assertEqual(dotenv_values("e.env"), values)

    def test_backup(self):
        p = Path("oci.env")
        p.write_text("old")
        setup_wizard.backup_and_write(p, "new")
        self.assertEqual((p.read_text(), Path("oci.env.bak").read_text()), ("new", "old"))


if __name__ == "__main__":
    unittest.main()
