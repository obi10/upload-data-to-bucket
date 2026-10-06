import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from upload_data_to_bucket.device import INVENTORY_SCRIPT, POWERSHELL, find_device_roots, select_device_roots

NAME = "TOSHIBA EXTERNAL_USB USB Device"
SERIAL = "F16141001102202"


def disk(**kwargs):
    result = dict(DiskNumber=1, FriendlyName="TOSHIBA EXTERNAL_USB", Model=NAME,
                  SerialNumbers=[SERIAL], IsBoot=False, IsSystem=False, IsOffline=False,
                  MappingSucceeded=True, Mounts=["D:\\"])
    result.update(kwargs)
    return result


class DeviceTests(unittest.TestCase):
    def select(self, records, serial=SERIAL):
        with patch.object(Path, "is_dir", return_value=True):
            return select_device_roots(records, NAME, serial)

    def test_matches_serial_despite_changed_name_and_letter(self):
        roots, _ = self.select([disk(FriendlyName="Different", Model="Different", Mounts=["Z:\\"])])
        self.assertEqual(roots, [Path("Z:\\")])

    def test_name_fallback_normalizes_usb_suffix(self):
        roots, _ = self.select([disk(Model="", SerialNumbers=[])], serial="")
        self.assertEqual(roots, [Path("D:\\")])

    def test_model_provider_fallback(self):
        roots, _ = self.select([disk(FriendlyName="", SerialNumbers=["", SERIAL])])
        self.assertEqual(roots, [Path("D:\\")])

    def test_same_model_different_serial_is_rejected(self):
        roots, _ = self.select([disk(SerialNumbers=["OTHER"])])
        self.assertEqual(roots, [])

    def test_missing_serial_does_not_fallback_to_name(self):
        self.assertEqual(self.select([disk(SerialNumbers=[])])[0], [])

    def test_serial_padding_is_normalized(self):
        self.assertEqual(self.select([disk(SerialNumbers=[" " + SERIAL.lower() + "\x00 "])])[0], [Path("D:\\")])

    def test_system_and_boot_disks_are_rejected(self):
        for field in ("IsBoot", "IsSystem"):
            with self.subTest(field=field):
                roots, decisions = self.select([disk(**{field: True})])
                self.assertEqual(roots, [])
                self.assertEqual(decisions[0]["reason"], "system_disk_rejected")

    def test_ambiguous_identity_fails(self):
        with self.assertRaisesRegex(RuntimeError, "Varios discos"):
            self.select([disk(), disk(DiskNumber=2, Mounts=["E:\\"])])

    def test_absent_disk(self):
        self.assertEqual(self.select([])[0], [])

    def test_unmounted_and_offline_are_explained(self):
        for record, reason in [(disk(Mounts=[]), "no_accessible_mount"), (disk(IsOffline=True), "disk_offline")]:
            roots, decisions = self.select([record])
            self.assertEqual(roots, [])
            self.assertEqual(decisions[0]["reason"], reason)

    def test_mapping_failure_is_not_absence(self):
        with self.assertRaisesRegex(RuntimeError, "particiones"):
            self.select([disk(MappingSucceeded=False)])

    def test_multiple_partitions_and_duplicate_provider_paths(self):
        roots, _ = self.select([disk(Mounts=["E:\\", "D:\\", "D:\\"])])
        self.assertEqual(roots, [Path("D:\\"), Path("E:\\")])

    def test_unlettered_folder_mount(self):
        self.assertEqual(self.select([disk(Mounts=["C:\\Mounts\\Toshiba\\"])])[0], [Path("C:\\Mounts\\Toshiba")])

    def test_inaccessible_mount(self):
        with patch.object(Path, "is_dir", side_effect=PermissionError("denied")):
            roots, decisions = select_device_roots([disk()], NAME, SERIAL)
        self.assertEqual(roots, [])
        self.assertIn("denied", decisions[0]["mount_checks"][0]["error"])

    def test_cannot_complete_when_one_volume_is_inaccessible(self):
        with patch.object(Path, "is_dir", side_effect=[True, False]):
            with self.assertRaisesRegex(RuntimeError, "inaccesibles"):
                select_device_roots([disk(Mounts=["D:\\", "E:\\"])], NAME, SERIAL)

    def test_real_inventory_shape_and_append_diagnostics(self):
        inventory = dict(InventorySucceeded=True, Disks=[disk()], Errors=["Get-Disk: unavailable"])
        result = subprocess.CompletedProcess([], 0, json.dumps(inventory), "")
        with tempfile.TemporaryDirectory() as temp, patch("upload_data_to_bucket.device.subprocess.run", return_value=result), patch.object(Path, "is_dir", return_value=True):
            log = Path(temp) / "detection.jsonl"
            for _ in range(2):
                self.assertEqual(find_device_roots(NAME, serial_number=SERIAL, diagnostic_path=log), [Path("D:\\")])
            records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["status"], "found")
            self.assertEqual(records[0]["inventory"]["Errors"], inventory["Errors"])

    def test_provider_failure_and_timeout_are_logged(self):
        failures = [subprocess.TimeoutExpired("powershell", 90), subprocess.CalledProcessError(1, "powershell", stderr="access denied")]
        for error in failures:
            with tempfile.TemporaryDirectory() as temp, patch("upload_data_to_bucket.device.subprocess.run", side_effect=error):
                log = Path(temp) / "detection.jsonl"
                with self.assertRaises(type(error)):
                    find_device_roots(NAME, diagnostic_path=log)
                self.assertEqual(json.loads(log.read_text(encoding="utf-8"))["status"], "error")

    def test_both_inventory_providers_failed_is_not_absence(self):
        result = subprocess.CompletedProcess([], 0, json.dumps(dict(InventorySucceeded=False, Disks=[], Errors=["denied"])), "")
        with patch("upload_data_to_bucket.device.subprocess.run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "enumerar"):
                find_device_roots(NAME)

    @unittest.skipUnless(POWERSHELL.exists(), "Requires Windows PowerShell")
    def test_powershell_cim_fallback_when_storage_provider_fails(self):
        # Execute the actual inventory script; only the Windows provider boundary is simulated.
        providers = r'''
function Get-Disk { throw 'Storage unavailable' }
function Get-Partition { throw 'Storage unavailable' }
function Get-CimInstance { [pscustomobject]@{Index=1;Model='TOSHIBA EXTERNAL_USB USB Device';SerialNumber='F16141001102202';PNPDeviceID='USBSTOR\test'} }
function Get-CimAssociatedInstance($InputObject, $Association) {
    if($Association -eq 'Win32_DiskDriveToDiskPartition') { [pscustomobject]@{BootPartition=$true} }
    else { [pscustomobject]@{DeviceID='Z:'} }
}
'''
        result = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", providers + INVENTORY_SCRIPT],
                                check=True, capture_output=True, text=True, encoding="utf-8", timeout=30)
        inventory = json.loads(result.stdout)
        self.assertTrue(inventory["InventorySucceeded"])
        self.assertEqual(len(inventory["Errors"]), 2)
        record = inventory["Disks"][0]
        self.assertTrue(record["MappingSucceeded"])
        # An active data partition must not be mistaken for the Windows boot disk.
        self.assertFalse(record["IsBoot"])
        self.assertEqual(self.select([record])[0], [Path("Z:\\")])

    @unittest.skipUnless(POWERSHELL.exists(), "Requires Windows PowerShell")
    def test_powershell_joins_both_provider_names(self):
        providers = r'''
function Get-Disk { [pscustomobject]@{Number=1;FriendlyName='TOSHIBA EXTERNAL_USB';SerialNumber='F16141001102202';IsBoot=$false;IsSystem=$false;BusType='USB'} }
function Get-Partition { [pscustomobject]@{DiskNumber=1;DriveLetter='E'} }
function Get-CimInstance { [pscustomobject]@{Index=1;Model='TOSHIBA EXTERNAL_USB USB Device';SerialNumber=' F16141001102202 ';PNPDeviceID='USBSTOR\test'} }
function Get-CimAssociatedInstance($InputObject, $Association) {
    if($Association -eq 'Win32_DiskDriveToDiskPartition') { [pscustomobject]@{BootPartition=$false} }
    else { [pscustomobject]@{DeviceID='E:'} }
}
'''
        result = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", providers + INVENTORY_SCRIPT],
                                check=True, capture_output=True, text=True, encoding="utf-8", timeout=30)
        inventory = json.loads(result.stdout)
        self.assertEqual(inventory["Errors"], [])
        self.assertEqual(len(inventory["Disks"]), 1)
        record = inventory["Disks"][0]
        self.assertEqual(record["Mounts"], ["E:\\"])
        self.assertEqual(record["Model"], NAME)
        self.assertEqual(self.select([record])[0], [Path("E:\\")])


if __name__ == "__main__":
    unittest.main()
