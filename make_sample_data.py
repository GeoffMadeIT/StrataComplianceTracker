# Version: 1.3.2
"""Generate synthetic exports (no real data) that exercise: renamed/reordered headers, device group in
the serial, added lines, removed lines, and a replaced device serial."""
import csv
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent / "samples"
V1 = ["BPC Title", "Device Serial", "Bpc Id", "Product", "Object Type", "Severity", "Location",
      "Remediation Location", "Object Id", "Bpc Status", "User Excluded"]
V2 = ["Bpc Id", "BPC Title", "Control Groups", "Device Serial", "Device Type", "Object Type", "Severity",
      "Location", "Remediation Location", "Object Id", "Bpc Status", "User Excluded"]
SEV = ["Critical", "High", "Medium", "Low", "Warning"]
OBJ = ["device_setup_general", "zone_protection_profile", "device_setup_management_interface"]
A, B, C, D = "12345678901234", "23456789012345", "34567890123456", "45678901234567"


def rows(devices, bpcs, pass_rate, seed, v2, serial_label=None):
    rnd = random.Random(seed)
    out = []
    for dev in devices:
        for b in bpcs:
            obj = f"obj-{b % 7}"
            status = "pass" if rnd.random() < pass_rate else ("exception" if rnd.random() < 0.1 else "fail")
            ser = (serial_label or {}).get(dev, dev)
            r = {"BPC Title": f"Control {b}", "Device Serial": ser, "Bpc Id": b, "Object Type": OBJ[b % 3],
                 "Severity": SEV[b % 5], "Location": "vsys1",
                 "Remediation Location": f"tsgid:111/Device:{dev}/Device Group:dg{b % 3}/Object Type:{OBJ[b % 3]}/",
                 "Object Id": obj, "Bpc Status": status, "User Excluded": "" if b % 11 else "True"}
            if v2:
                r.update({"Device Type": "ngfw", "Control Groups": f"NIST CSF; CIS Controls v8; 800-53 AC-{b % 6}"})
            else:
                r["Product"] = "ngfw"
            out.append(r)
    return out


def write(name, data, header):
    with open(OUT / name, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(data)


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for old in OUT.glob("*.csv"):
        old.unlink()
    r = range
    # 2026-08-13  v1 headers: devices A, B, C, controls 1-40
    write("nist_csf_2026-08-13.csv", rows([A, B, C], r(1, 41), 0.45, 1, False), V1)
    # 2026-09-10  v1 headers: A loses controls 5-8 (they come back later), C loses 36-40 for good, 41-45 added
    d2 = rows([A], [b for b in r(1, 46) if b not in (5, 6, 7, 8)], 0.55, 2, False)
    d2 += rows([B], r(1, 46), 0.55, 3, False)
    d2 += rows([C], [b for b in r(1, 46) if b not in r(36, 41)], 0.55, 4, False)
    write("nist_csf_2026-09-10.csv", d2, V1)
    # 2026-10-01  v2 headers (Product -> Device Type, Control Groups added, reordered); B replaced by serial D;
    #             C's serial now carries a device group name; A's controls 5-8 return
    d3 = rows([A], r(1, 46), 0.7, 5, True)
    d3 += rows([D], r(1, 46), 0.7, 6, True)
    d3 += rows([C], [b for b in r(1, 46) if b not in r(36, 41)], 0.7, 7, True, serial_label={C: f"{C} Branch-FW"})
    write("nist_csf_2026-10-01.csv", d3, V2)
    print("wrote", sorted(p.name for p in OUT.glob("*.csv")))
