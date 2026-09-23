#!/usr/bin/env python3
"""Two-way sync of the Cue library between the iPhone and the Mac app.

Run by launchd every two minutes (com.sethcosmo.cue-sync). Does nothing unless the phone is connected (USB; devicectl).
Merges both library.json files: per mix the newer `modified` wins, playback position follows the later `lastPlayed`,
a mix removed on one side since the last sync is removed on the other (into its Recently removed), new mixes and their
audio are copied across. Nothing here writes library.json: each side gets a `sync-inbox.json` next to its library that
the app applies itself at a safe moment (launch, foreground, and every minute on the Mac), so a running app is never
clobbered mid-save.

    sync.py                 # normal run
    sync.py --dry-run       # print the plan, write nothing, copy nothing
    sync.py --phone-dir P --mac-dir M   # merge two local folders (tests), no devicectl
"""
import fcntl, json, os, shutil, subprocess, sys, time, uuid
from datetime import datetime, timezone

DEVICE = "9BD90016-EE94-564C-98C4-A925F6B1F94E"
BUNDLE = "com.sethcosmo.Cue"
MAC_DIR = os.path.expanduser("~/Library/Containers/EAD0DFBB-9461-4016-B3F3-7FF96073A2D4/Data/Library/Application Support/Cue")
WORK = os.path.expanduser("~/Mixtapes/cue-sync")
STATE = os.path.join(WORK, "state.json")
LOG = os.path.join(WORK, "sync.log")
PHONE_STAGE = os.path.join(WORK, "phone")
REF = datetime(2001, 1, 1, tzinfo=timezone.utc)   # JSONEncoder's default date: seconds since 2001


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    os.makedirs(WORK, exist_ok=True)
    with open(LOG, "a") as f: f.write(line + "\n")


def now_ref(): return (datetime.now(timezone.utc) - REF).total_seconds()


def devicectl(*args, quiet=True):
    cmd = ["xcrun", "devicectl", *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 and not quiet: log(f"devicectl failed: {' '.join(args[:3])}: {r.stderr.strip()[:200]}")
    return r.returncode == 0


def phone_connected():
    out = os.path.join(WORK, "devices.json")
    if not devicectl("list", "devices", "--json-output", out): return False
    try:
        for d in json.load(open(out))["result"]["devices"]:
            if d["identifier"] == DEVICE:
                c = d.get("connectionProperties", {})
                return c.get("tunnelState") == "connected" and c.get("pairingState") == "paired"
    except Exception as e:
        log(f"device list unreadable: {e}")
    return False


def pull(rel, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    return devicectl("device", "copy", "from", "--device", DEVICE, "--source", f"Library/Application Support/Cue/{rel}",
                     "--destination", dest, "--domain-type", "appDataContainer", "--domain-identifier", BUNDLE, quiet=False)


def push(src, rel):
    return devicectl("device", "copy", "to", "--device", DEVICE, "--source", src, "--destination", f"Library/Application Support/Cue/{rel}",
                     "--domain-type", "appDataContainer", "--domain-identifier", BUNDLE, quiet=False)


def read_json(path, default):
    try: return json.load(open(path))
    except Exception: return default


def read_library(path, name):
    """A library that cannot be read is NOT an empty library. None means: refuse to act on this side."""
    if not os.path.exists(path): log(f"{name} library.json missing at {path}"); return None
    try:
        data = json.load(open(path))
    except Exception as e:
        log(f"{name} library.json unreadable: {e}"); return None
    if not isinstance(data, list): log(f"{name} library.json is not a list"); return None
    return data


MAX_REMOVALS_FRACTION = 0.10   # a real removal is one or two mixes; anything bigger is a misread and is refused


class Side:
    """One device's view: records by id, trash by id, and how to read/write its files."""
    def __init__(self, name, local_dir, remote):
        self.name, self.dir, self.remote = name, local_dir, remote
        lib = read_library(os.path.join(local_dir, "library.json"), name)
        self.readable = lib is not None
        self.records = {m["id"]: m for m in (lib or [])}
        self.trash = {t["mix"]["id"]: t for t in read_json(os.path.join(local_dir, "trash.json"), [])}
        self.inbox = {"records": [], "removed": []}
    def has_audio(self, m):
        return not m.get("fileName") or os.path.exists(os.path.join(self.dir, "Audio", m["fileName"]))


def content(m):
    c = dict(m); c.pop("position", None); c.pop("lastPlayed", None); c.pop("modified", None); return c


def merge(phone, mac, state, dry):
    """Fill each side's inbox and the list of audio files to copy. Returns (copies, summary)."""
    known = set(state.get("ids", []))          # ids present in the merged library after the last sync
    copies = []                                # (from_side, to_side, fileName)
    summary = {"phone": 0, "mac": 0, "removed": 0, "audio": 0}
    ids = set(phone.records) | set(mac.records)
    for mid in ids:
        a, b = phone.records.get(mid), mac.records.get(mid)
        if a and b:
            ma, mb = a.get("modified", -1e12), b.get("modified", -1e12)
            same = content(a) == content(b)
            if same: winner = None
            elif ma != mb: winner = a if ma > mb else b
            else: winner = a if len(a.get("markers", [])) >= len(b.get("markers", [])) else b   # legacy: the richer record
            la, lb = a.get("lastPlayed", -1e12), b.get("lastPlayed", -1e12)
            later = None if la == lb else (a if la > lb else b)
            for side, own in ((phone, a), (mac, b)):
                if (winner is None or winner is own) and (later is None or later is own): continue
                rec = dict(winner or own)
                src = later or own
                rec["lastPlayed"], rec["position"] = src.get("lastPlayed"), src.get("position", 0)
                side.inbox["records"].append(rec); summary[side.name] += 1
        else:
            present, absent = (phone, mac) if a else (mac, phone)
            rec = a or b
            pending = state.get("removed", {}).get(mid)              # a removal issued earlier that this side has not applied yet
            removed_at = absent.trash.get(mid, {}).get("removed")
            if pending is not None or mid in known or removed_at is not None:
                stamp = removed_at if removed_at is not None else (pending if pending is not None else state.get("last_run", now_ref()))
                if rec.get("modified", -1e12) > stamp:               # edited after the removal: it comes back
                    absent.inbox["records"].append(rec); summary[absent.name] += 1
                    if rec.get("fileName") and not absent.has_audio(rec): copies.append((present, absent, rec["fileName"]))
                    state.get("removed", {}).pop(mid, None)
                else:
                    present.inbox["removed"].append({"id": mid, "removed": stamp}); summary["removed"] += 1
                    state.setdefault("removed", {})[mid] = stamp
            else:
                absent.inbox["records"].append(rec); summary[absent.name] += 1
                if rec.get("fileName") and not absent.has_audio(rec): copies.append((present, absent, rec["fileName"]))
    for mid in list(state.get("removed", {})):                 # gone from both libraries: nothing left to remove
        if mid not in phone.records and mid not in mac.records: state["removed"].pop(mid)
    return copies, summary


def copy_audio(src_side, dst_side, name, dry):
    if dry: return True
    if src_side.remote and not dst_side.remote:      # phone → Mac
        tmp = os.path.join(PHONE_STAGE, "Audio", name)
        if not pull(f"Audio/{name}", tmp): return False
        os.makedirs(os.path.join(dst_side.dir, "Audio"), exist_ok=True)
        shutil.move(tmp, os.path.join(dst_side.dir, "Audio", name)); return True
    if dst_side.remote and not src_side.remote:      # Mac → phone
        return push(os.path.join(src_side.dir, "Audio", name), f"Audio/{name}")
    shutil.copy2(os.path.join(src_side.dir, "Audio", name), os.path.join(dst_side.dir, "Audio", name)); return True   # local test dirs


def write_inbox(side, dry):
    if not side.inbox["records"] and not side.inbox["removed"]: return
    path = os.path.join(side.dir, "sync-inbox.json")
    if dry:
        log(f"  {side.name}: would write inbox with {len(side.inbox['records'])} records, {len(side.inbox['removed'])} removals"); return
    os.makedirs(side.dir, exist_ok=True)
    json.dump(side.inbox, open(path, "w"))
    if side.remote: push(path, "sync-inbox.json")


def main():
    dry = "--dry-run" in sys.argv
    args = dict(zip(sys.argv[1:], sys.argv[2:]))
    local_test = "--phone-dir" in args
    os.makedirs(WORK, exist_ok=True)
    lock = open(os.path.join(WORK, "lock"), "w")
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError: return                                  # another run is in progress
    if local_test:
        phone = Side("phone", args["--phone-dir"], remote=False); mac = Side("mac", args["--mac-dir"], remote=False)
        state_path = os.path.join(args["--mac-dir"], "sync-state.json")
    else:
        if not phone_connected(): return
        if not pull("library.json", os.path.join(PHONE_STAGE, "library.json")): log("phone library unreadable (locked?)"); return
        trash = os.path.join(PHONE_STAGE, "trash.json")
        if os.path.exists(trash): os.remove(trash)
        devicectl("device", "copy", "from", "--device", DEVICE, "--source", "Library/Application Support/Cue/trash.json", "--destination", trash,
                  "--domain-type", "appDataContainer", "--domain-identifier", BUNDLE)   # absent until something was removed
        phone = Side("phone", PHONE_STAGE, remote=True); mac = Side("mac", MAC_DIR, remote=False)
        state_path = STATE
    if not phone.readable or not mac.readable:
        log("refusing to sync: a side could not be read" + ("" if mac.readable else " (Mac container unreadable: this process needs Full Disk Access)")); return
    if (phone.records and not mac.records) or (mac.records and not phone.records):
        log(f"refusing to sync: one side is empty (phone {len(phone.records)}, mac {len(mac.records)}); restore it by hand first"); return
    if not phone.records and not mac.records: return
    state = read_json(state_path, {})
    copies, summary = merge(phone, mac, state, dry)
    total = max(len(phone.records), len(mac.records))
    if summary["removed"] > max(3, int(total * MAX_REMOVALS_FRACTION)):
        log(f"refusing to sync: {summary['removed']} removals against {total} mixes looks like a misread, nothing written"); return
    if not any(summary.values()):
        state.update({"ids": sorted((set(phone.records) | set(mac.records)) - set(state.get("removed", {}))), "last_run": now_ref()})
        if not dry: json.dump(state, open(state_path, "w"))
        return
    log(f"merge: phone gets {summary['phone']}, mac gets {summary['mac']}, removals {summary['removed']}, audio copies {len(copies)}{' (dry run)' if dry else ''}")
    failed = set()
    for src, dst, name in copies:
        log(f"  copy {name} {src.name} → {dst.name}")
        if not copy_audio(src, dst, name, dry): failed.add(name); log(f"  FAILED {name}")
    for side in (phone, mac):   # a record whose audio did not arrive is left out; the next run retries the copy
        side.inbox["records"] = [r for r in side.inbox["records"] if r.get("fileName") not in failed]
        write_inbox(side, dry)
    merged = (set(phone.records) | set(mac.records)) - set(state.get("removed", {}))
    state.update({"ids": sorted(merged), "last_run": now_ref()})
    if not dry: json.dump(state, open(state_path, "w"))


if __name__ == "__main__":
    main()
