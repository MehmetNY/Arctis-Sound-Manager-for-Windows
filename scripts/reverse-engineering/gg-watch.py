#!/usr/bin/env python3
"""Watch for a new SteelSeries GG release, and act on what changed.

Run it on a timer. It costs one HEAD request when nothing is new.

What it does when a new version appears:

  1. downloads the installer and extracts the two things worth reading —
     the encrypted device specs, and Sonar's db-migrations;
  2. recovers the descriptor passphrase from SteelSeriesEngine.exe and
     decrypts the Arctis specs (recover-key.py + decode-arctis-specs.sh,
     both already here), then disassembles the engine builtins those specs
     call and SSEdevice.dll's HID transport (gg-disasm.py), and decompiles
     Sonar's .NET assemblies to C# (ilspycmd);
  3. compares against the previous version using fingerprint index:
       * new .edevice files            → new hardware
       * changed Arctis .device files  → protocol changes worth reading
       * changed builtins / transport  → GG now builds or sends bytes differently
       * changed Sonar C#              → EQ / ChatMix / routing logic moved
       * presets in the catalogue that ASM does not ship
  4. acts:
       * presets  → writes the JSON into ASM, commits to main (the branch
                    preset_sync reads from). The back-merge workflow carries
                    it to develop. No cherry-pick needed.
       * hardware or protocol changes → opens a GitHub issue, because those
                    need a human to read the GoLisp and decide.

Why the two outcomes differ: a preset is data in a shape we already know and
can be added mechanically. A spec change is a question — "does this opcode
mean what I think" — and answering it wrong writes silent garbage to somebody's
headset. So that half stops at "here is what changed, go look".

Nothing SteelSeries ships is committed to *this* repository: the decrypted
specs reach git only in the private loteran/steelseries-research repo (a
separate destination, its own token — see push_specs_to_research_repo), kept
there so any machine can clone the specs instead of re-running the extraction.
Only the preset JSON — the same data ASM already carries — reaches this repo.

Copyright (C) 2026 loteran — SPDX-License-Identifier: GPL-3.0-or-later
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

# ── Paths (configurable via environment) ─────────────────────────────────────

GG_WORKDIR = Path(os.environ.get("GG_WORKDIR", Path.home() / "steelseries-research"))
ASM_ROOT   = Path(os.environ.get("ASM_ROOT",   Path.home() / "Arctis-Sound-Manager"))

PRESETS     = ASM_ROOT / "src/arctis_sound_manager/gui/presets"
PROVENANCE  = ASM_ROOT / "presets_provenance.json"
STATE_FILE  = ASM_ROOT / ".github/gg-watch-state.json"
MANIFEST    = ASM_ROOT / "presets_manifest.json"
GENERATE_MANIFEST = ASM_ROOT / "scripts/generate_presets_manifest.py"

REPO = "loteran/Arctis-Sound-Manager"
RESEARCH_REPO = "loteran/steelseries-research"

# Sonar's own .NET assemblies — EQ, ChatMix, routing, config models. The
# runtime and third-party libraries next to them are left out. A native one
# (the APO / DSP layer) has no IL to decompile and is skipped.
SONAR_ASSEMBLIES = ("SteelSeriesSonar", "Sonar.*", "SoundStage.*", "Interop.*")

LATEST_URL = "https://steelseries.com/gg/downloads/gg/latest/windows"
VERSION_RE = re.compile(r"SteelSeriesGG([\d.]+)Setup\.exe")

log = lambda *a: print(*a, flush=True)

# Set by --dry-run: do every read and comparison, skip everything that writes
# outward (git push, gh issue). Without it there is no way to exercise the
# "new version" path without publishing something.
DRY_RUN = False

# Set by --no-push: for local use where the user manages git themselves.
NO_PUSH = False


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


# ── version discovery ────────────────────────────────────────────────────────

def latest_version() -> tuple[str, str]:
    """(version, url) for the current GG installer, without downloading it.

    The download page 302s to a filename carrying the version, so a HEAD and
    the final URL are enough to know whether there is anything to do.
    """
    out = subprocess.run(
        ["curl", "-sIL", "--max-time", "60", "-o", "/dev/null",
         "-w", "%{url_effective}", LATEST_URL],
        check=True, text=True, capture_output=True).stdout.strip()
    m = VERSION_RE.search(out)
    if not m:
        raise SystemExit(f"could not read a version out of {out!r}")
    return m.group(1), out


def _vkey(v: str) -> tuple:
    """Version as a 3-tuple, so "117" and "117.0.0" compare equal."""
    parts = [int(x) for x in re.findall(r"\d+", v)][:3]
    return tuple(parts + [0] * (3 - len(parts)))


# ── state management ─────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.is_file():
        return json.loads(STATE_FILE.read_text())
    return {"version": None, "edevice_files": [], "spec_hashes": {}}


def save_state(version: str, edevice_files: list[str], spec_hashes: dict[str, str],
               presets_added: list[str], new_files: list[str],
               changed_specs: list[str], issue: str | None,
               disasm_fingerprints: dict[str, str], changed_code: list[str],
               sonar_fingerprints: dict[str, str], changed_sonar_ns: list[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        "version": version,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "edevice_files": sorted(edevice_files),
        "spec_hashes": dict(sorted(spec_hashes.items())),
        "presets_added": presets_added,
        "new_files": new_files,
        "changed_specs": changed_specs,
        "disasm_fingerprints": dict(sorted(disasm_fingerprints.items())),
        "changed_code": changed_code,
        "sonar_fingerprints": dict(sorted(sonar_fingerprints.items())),
        "changed_sonar": changed_sonar_ns,
        "issue": issue,
    }, indent=2, ensure_ascii=False) + "\n")


def commit_state() -> None:
    """Push the freshly-written STATE_FILE to main, on its own.

    Not folded into add_presets()'s commit: that one only fires when new
    presets were found, which most version bumps don't produce. Without
    this, a run that changes nothing but the tracked version writes
    STATE_FILE to the runner's ephemeral checkout and it is never pushed —
    next week's run reads the same stale (or absent) state and reprocesses
    the "new" version all over again forever.
    """
    if NO_PUSH:
        log("  [no-push] state file written — commit manually")
        return
    git = ["git", "-C", str(ASM_ROOT)]
    status = subprocess.run(git + ["status", "--porcelain", "--", str(STATE_FILE)],
                             text=True, capture_output=True).stdout
    if not status.strip():
        return
    run(git + ["add", str(STATE_FILE)])
    run(git + ["commit", "-m", "chore(gg-watch): update state [skip ci]"])
    run(git + ["push", "origin", "HEAD"])
    log("  state file pushed to main")


# ── acquisition ──────────────────────────────────────────────────────────────

def fetch(version: str, url: str) -> Path:
    GG_WORKDIR.mkdir(parents=True, exist_ok=True)
    dest = GG_WORKDIR / f"SteelSeriesGG{version}Setup.exe"
    if dest.is_file() and dest.stat().st_size > 100_000_000:
        log(f"  installer already downloaded: {dest.name}")
        return dest
    log(f"  downloading {dest.name} …")
    run(["curl", "-fsSL", "--max-time", "3600", "-o", str(dest), url])
    return dest


def extract(installer: Path, version: str) -> Path:
    root = GG_WORKDIR / f"gg-{version}"
    if (root / "apps/engine/deviceSpecifications").is_dir():
        log(f"  already extracted into {root.name}")
        return root
    root.mkdir(parents=True, exist_ok=True)
    run(["7z", "x", "-y", f"-o{root}", str(installer),
         "apps/engine/deviceSpecifications/*",
         "apps/engine/SteelSeriesEngine.exe",
         "apps/engine/SSEdevice.dll",
         "apps/sonar/db-migrations/*",
         *(f"apps/sonar/{pattern}.dll" for pattern in SONAR_ASSEMBLIES)])
    return root


def decode(root: Path, version: str) -> Path:
    out = GG_WORKDIR / f"decoded-{version}"
    if out.is_dir() and any(out.iterdir()):
        log(f"  already decoded into {out.name}")
        return out
    key = GG_WORKDIR / f"key-{version}.txt"
    exe = root / "apps/engine/SteelSeriesEngine.exe"
    log("  recovering the descriptor passphrase …")
    key.write_text(run([sys.executable, str(Path(__file__).parent / "recover-key.py"),
                        str(exe)]).stdout)
    if len(key.read_text().strip()) != 128:
        raise SystemExit("passphrase is not 128 chars — the Go layout probably "
                         "moved; redo the offsets by hand (see the memory)")
    log("  decrypting the Arctis specs …")
    run([str(Path(__file__).parent / "decode-arctis-specs.sh"),
         str(root / "apps/engine/deviceSpecifications"), str(key), str(out)])
    return out


def disassemble(root: Path, decoded: Path, version: str) -> Path | None:
    """Disassemble the GG code the specs lean on (see gg-disasm.py)."""
    out = GG_WORKDIR / f"disasm-{version}"
    if (out / "fingerprints.json").is_file():
        log(f"  already disassembled into {out.name}")
        return out
    log("  disassembling the engine builtins and the HID transport …")
    try:
        run([sys.executable, str(Path(__file__).parent / "gg-disasm.py"),
             str(root / "apps/engine/SteelSeriesEngine.exe"),
             str(root / "apps/engine/SSEdevice.dll"), str(decoded), str(out)])
    except (subprocess.CalledProcessError, OSError) as e:
        # A layout change in GG must not cost the presets and the spec diff.
        log(f"  disassembly failed, skipped: {e}")
        return None
    return out


def decompile_sonar(root: Path, version: str) -> Path | None:
    """Decompile Sonar's .NET assemblies to C# with ilspycmd."""
    out = GG_WORKDIR / f"sonar-{version}"
    if (out / "fingerprints.json").is_file():
        log(f"  already decompiled into {out.name}")
        return out
    if not shutil.which("ilspycmd"):
        log("  ilspycmd not installed — skipping the Sonar decompilation")
        return None
    log("  decompiling Sonar …")
    out.mkdir(parents=True, exist_ok=True)
    dlls = sorted({dll for pattern in SONAR_ASSEMBLIES
                   for dll in (root / "apps/sonar").glob(f"{pattern}.dll")})
    for dll in dlls:
        try:
            run(["ilspycmd", "-p", "-o", str(out / dll.stem), str(dll)])
        except subprocess.CalledProcessError:
            log(f"    {dll.name}: native, not .NET — skipped")
            shutil.rmtree(out / dll.stem, ignore_errors=True)
    # .cs only: the generated .csproj carries a random project GUID.
    fingerprints = {str(f.relative_to(out)): hashlib.sha256(f.read_bytes()).hexdigest()[:16]
                    for f in sorted(out.rglob("*.cs"))}
    (out / "fingerprints.json").write_text(json.dumps(fingerprints, indent=1) + "\n")
    return out


def changed_sonar(sonar: Path | None, state: dict) -> list[str]:
    """Sonar namespaces (directories) with an added, removed or changed file."""
    if sonar is None or not state.get("sonar_fingerprints"):
        return []
    old = state["sonar_fingerprints"]
    new = json.loads((sonar / "fingerprints.json").read_text())
    files = {f for f in old.keys() | new.keys() if old.get(f) != new.get(f)}
    return sorted({str(Path(f).parent) for f in files})


def changed_disassembly(disasm: Path | None, state: dict) -> list[str]:
    """Builtins / transport code whose fingerprint moved since last version."""
    if disasm is None:
        return []
    old = state.get("disasm_fingerprints", {})
    new = json.loads((disasm / "fingerprints.json").read_text())
    if not old:
        return []
    return sorted(n for n, h in new.items() if old.get(n) != h)


# ── comparison (against state, not disk) ─────────────────────────────────────

def build_index(root: Path, decoded: Path) -> tuple[list[str], dict[str, str]]:
    """Return (edevice filenames, {decoded_name: sha256})."""
    specs = root / "apps/engine/deviceSpecifications"
    edevice_files = sorted(p.name for p in specs.glob("*.edevice")) if specs.is_dir() else []
    spec_hashes = {}
    if decoded.is_dir():
        for p in decoded.iterdir():
            if p.is_file():
                spec_hashes[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return edevice_files, dict(sorted(spec_hashes.items()))


def compare_to_state(edevice_files: list[str], spec_hashes: dict[str, str],
                     state: dict) -> tuple[list[str], list[str]]:
    """Compare current index against state, return (new_edevice, changed_specs).

    First run (empty state) returns ([], []) — adopt silently, don't spam.
    """
    old_edevice = set(state.get("edevice_files", []))
    old_hashes = state.get("spec_hashes", {})

    # First run: nothing to compare against, treat as adopt.
    if not old_edevice and not old_hashes:
        return [], []

    new_files = sorted(set(edevice_files) - old_edevice)

    # Changed = a hash that differs. A decoded file the old index never
    # tracked has nothing to differ from: GG 122 (#341) reported 15 shared
    # specs as changed that were byte-identical to 121's, only newly decoded.
    # A spec that is genuinely new arrives with its .edevice, in new_files.
    changed = sorted(name for name, h in spec_hashes.items()
                     if name in old_hashes and old_hashes[name] != h)

    return new_files, changed


INSERT = re.compile(
    r"INSERT\s+INTO\s+configs\s*\((?P<cols>.*?)\)\s*VALUES\s*\((?P<vals>.*?)\);",
    re.S | re.I)
UPDNAME = re.compile(
    r"UPDATE\s+configs\s+SET\s+name\s*=\s*(?P<name>'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\")"
    r"\s+WHERE\s+id\s*=\s*'(?P<id>[0-9a-f-]+)'", re.S | re.I)


def _split_vals(s: str) -> list[str]:
    out, buf, q, i = [], [], None, 0
    while i < len(s):
        c = s[i]
        if q:
            if c == q:
                if i + 1 < len(s) and s[i + 1] == q:
                    buf.append(c); i += 2; continue
                q = None
            buf.append(c)
        elif c in "'\"":
            q = c; buf.append(c)
        elif c == ",":
            out.append("".join(buf).strip()); buf = []
        else:
            buf.append(c)
        i += 1
    out.append("".join(buf).strip())
    return out


def _unq(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1].replace(v[0] * 2, v[0])
    return v


def catalogue(root: Path) -> dict[str, dict]:
    """Every Sonar preset in the migrations, with later renames applied."""
    mig = root / "apps/sonar/db-migrations"
    by_id, data_of = {}, {}
    for sql in sorted(mig.rglob("*.sql")):
        t = sql.read_text(encoding="utf-8", errors="replace")
        for m in INSERT.finditer(t):
            cols = [c.strip().strip('"`') for c in m.group("cols").split(",")]
            vals = _split_vals(m.group("vals"))
            if len(cols) != len(vals):
                continue
            row = dict(zip(cols, vals))
            if not {"id", "name", "data"} <= row.keys():
                continue
            try:
                payload = json.loads(_unq(row["data"]))
            except Exception:
                continue
            pid = _unq(row["id"])
            by_id[pid] = _unq(row["name"])
            data_of[pid] = payload
        for m in UPDNAME.finditer(t):
            if m.group("id") in by_id:
                by_id[m.group("id")] = _unq(m.group("name"))
    return {name: data_of[pid] for pid, name in by_id.items()}


def _fold(n: str) -> str:
    """Compare on letters and digits only."""
    for ch in "™®©":
        n = n.replace(ch, "")
    n = "".join(c for c in unicodedata.normalize("NFD", n)
                if unicodedata.category(c) != "Mn").casefold()
    if n.startswith("sonar preset"):
        n = n[len("sonar preset"):]
    return "".join(c for c in n if c.isalnum())


def _words(n: str) -> frozenset:
    n = "".join(c for c in unicodedata.normalize("NFD", n)
                if unicodedata.category(c) != "Mn").casefold()
    for junk in ("sonar preset", " by ", ":", "-", "_", "™", "®"):
        n = n.replace(junk, " ")
    return frozenset(w for w in n.split() if w.isalnum())


def missing_presets(root: Path) -> dict[str, dict]:
    have, have_words = set(), []
    for f in PRESETS.glob("*.json"):
        m = re.match(r"^(.*) \[(Game|Chat|Mic)\]\.json$", f.name)
        if m:
            have.add(_fold(m.group(1)))
            have_words.append(_words(m.group(1)))

    def known(name: str) -> bool:
        if _fold(name) in have:
            return True
        w = _words(name)
        return any(w and a and (w <= a or a <= w) for a in have_words)

    return {n: d for n, d in catalogue(root).items() if not known(n)}


# ── actions ──────────────────────────────────────────────────────────────────

def _safe(name: str) -> str:
    """A filename every packager can unpack: ASCII only. bsdtar without a
    locale fails on anything else, which broke the AUR build (#132) — GG 121's
    "EA Sports FC™ 27" got through. Symbols go, accents fold to their letter."""
    for ch in "™®©":
        name = name.replace(ch, "")
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    for ch in ':/\\?*"<>|':
        name = name.replace(ch, "_")
    return " ".join(name.split())


def add_presets(new: dict[str, dict], version: str) -> list[str]:
    """Write presets, provenance, manifest, and commit on current branch (main)."""
    written = []
    for name, data in sorted(new.items()):
        chan = "Game" if data.get("formFactor") == "headphones" else "Chat"
        path = PRESETS / f"{_safe(name)} [{chan}].json"
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        written.append(path.name)

    if not written:
        return []

    if DRY_RUN:
        log("  [dry-run] would commit and push these to main")
        for n in written:
            (PRESETS / n).unlink(missing_ok=True)
        return written

    # Merge provenance
    try:
        prov = json.loads(PROVENANCE.read_text()) if PROVENANCE.is_file() else {}
    except Exception:
        prov = {}
    prov.update({n: version for n in written})
    PROVENANCE.write_text(
        json.dumps(dict(sorted(prov.items())), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")

    # Regenerate manifest — ESSENTIAL: GITHUB_TOKEN pushes don't trigger
    # on:push workflows, so update-presets-manifest.yml would not fire.
    run([sys.executable, str(GENERATE_MANIFEST)])

    if NO_PUSH:
        log("  [no-push] files written, manifest regenerated — commit manually")
        return written

    body = "\n".join(f"  {n}" for n in written)
    msg = (f"feat(presets): add {len(written)} Sonar preset(s) from GG {version}\n\n"
           f"Extracted from the Sonar db-migrations in the installer, where each\n"
           f"preset is an INSERT INTO configs carrying the same JSON shape ASM\n"
           f"stores on disk.\n\n{body}\n")
    # STATE_FILE is deliberately not added here: save_state() in main() only
    # writes it *after* this function returns, so committing it at this point
    # would still carry the previous run's content. commit_state() below is
    # what actually persists it, unconditionally, once it is current.
    git = ["git", "-C", str(ASM_ROOT)]
    run(git + ["add", str(PRESETS), str(PROVENANCE), str(MANIFEST)])
    run(git + ["commit", "-m", msg])
    run(git + ["push", "origin", "HEAD"])
    log("  presets pushed to main")
    return written


def push_specs_to_research_repo(decoded: Path, disasm: Path | None,
                                sonar: Path | None, version: str) -> bool:
    """Mirror this version's decrypted specs and disassembly into the private
    research repo.

    Separate destination from ASM's own repo on purpose (see the module
    docstring: nothing SteelSeries ships reaches *this* repository) —
    steelseries-research is where the decoded-<version>/ trees actually live,
    cloneable from any machine instead of re-running the extraction there.
    Needs its own token: ASM's GITHUB_TOKEN has no write access outside ASM,
    and deliberately shouldn't — RESEARCH_REPO_TOKEN is a separate,
    narrowly-scoped secret (contents:write on steelseries-research only).
    """
    if DRY_RUN or NO_PUSH:
        log(f"  [dry-run/no-push] would push decoded-{version} to {RESEARCH_REPO}")
        return False
    token = os.environ.get("RESEARCH_REPO_TOKEN")
    if not token:
        log("  RESEARCH_REPO_TOKEN not set — skipping steelseries-research push")
        return False

    clone_dir = GG_WORKDIR / "steelseries-research-clone"
    if clone_dir.is_dir():
        shutil.rmtree(clone_dir)
    url = f"https://x-access-token:{token}@github.com/{RESEARCH_REPO}.git"
    run(["git", "clone", "--depth", "1", url, str(clone_dir)])

    trees = [decoded] + [t for t in (disasm, sonar) if t]
    for tree in trees:
        dest = clone_dir / tree.name
        if dest.is_dir():
            shutil.rmtree(dest)
        shutil.copytree(tree, dest)

    git = ["git", "-C", str(clone_dir)]
    run(git + ["config", "user.name", "github-actions[bot]"])
    run(git + ["config", "user.email", "github-actions[bot]@users.noreply.github.com"])
    run(git + ["add"] + [tree.name for tree in trees])
    status = subprocess.run(git + ["status", "--porcelain"],
                             text=True, capture_output=True).stdout
    if not status.strip():
        log(f"  {RESEARCH_REPO} already has decoded-{version} — nothing to push")
        return False
    run(git + ["commit", "-m", f"feat: add decoded Arctis specs and disassembly for GG {version}\n\n"
               "Automated by ASM's gg-watch (scripts/reverse-engineering/gg-watch.py)."])
    run(git + ["push", "origin", "HEAD"])
    log(f"  decoded-{version} pushed to {RESEARCH_REPO}")
    return True


def open_issue(version: str, new_files: list[str], changed: list[str],
               changed_code: list[str], changed_sonar_ns: list[str]) -> str | None:
    if not new_files and not changed and not changed_code and not changed_sonar_ns:
        return None
    title = f"SteelSeries GG {version}: device specs changed"
    existing = subprocess.run(
        ["gh", "issue", "list", "--repo", REPO, "--state", "all",
         "--search", title, "--json", "title,url"],
        text=True, capture_output=True).stdout
    if title in existing:
        log("  issue already open for this version")
        return None

    lines = [f"GG **{version}** changes what the device specifications say. "
             f"Raised automatically; the reading is still a human job.", ""]
    if new_files:
        heads = [f for f in new_files
                 if any(k in f.lower() for k in ("arctis", "nova", "gamebud"))]
        lines += [f"### New device files ({len(new_files)})", ""]
        lines += [f"- `{f}`" + ("  **← headset**" if f in heads else "")
                  for f in new_files]
        lines += ["", "A headset here means a possible new YAML profile. Anything "
                  "else is a mouse, keyboard or controller and is out of scope "
                  "for ASM.", ""]
    if changed:
        lines += [f"### Changed Arctis specs ({len(changed)})", ""]
        lines += [f"- `{f}`" for f in changed]
        lines += ["", "Worth diffing against the previous version: these carry "
                  "opcodes, status layouts and screen geometry. A change here can "
                  "mean a new capability — or that one of ours moved.", ""]
    if changed_code:
        lines += [f"### Changed GG code the specs rely on ({len(changed_code)})", ""]
        lines += [f"- `{f}`" for f in changed_code]
        lines += ["", "The engine builtins (frame packing, conversions) or the HID "
                  "transport in SSEdevice.dll changed. Diff `disasm-<version>/` "
                  "in the research repo: ASM copies what these do.", ""]
    if changed_sonar_ns:
        lines += [f"### Changed Sonar code ({len(changed_sonar_ns)} namespaces)", ""]
        lines += [f"- `{f}`" for f in changed_sonar_ns[:40]]
        if len(changed_sonar_ns) > 40:
            lines += [f"- … and {len(changed_sonar_ns) - 40} more"]
        lines += ["", "Diff `sonar-<version>/` in the research repo: EQ, ChatMix "
                  "and routing logic that ASM's Sonar side mirrors.", ""]
    lines += ["<sub>Opened by the GG watcher. Decrypted specs are not attached: "
              "they are SteelSeries' material and stay off this repository.</sub>"]

    if DRY_RUN:
        log(f"  [dry-run] would open an issue: {title}")
        log("\n".join("      " + l for l in lines if l))
        return None

    out = run(["gh", "issue", "create", "--repo", REPO, "--title", title,
               "--body", "\n".join(lines)]).stdout.strip()
    return out.splitlines()[-1] if out else None


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    global DRY_RUN, NO_PUSH
    DRY_RUN = "--dry-run" in sys.argv[1:]
    NO_PUSH = "--no-push" in sys.argv[1:]
    forced = next((a.split("=", 1)[1] for a in sys.argv[1:]
                   if a.startswith("--since=")), None)

    for tool in ("curl", "7z", "gh", "git"):
        if not shutil.which(tool):
            raise SystemExit(f"missing required tool: {tool}")

    state = load_state()
    version, url = latest_version()
    known = forced or state.get("version")
    log(f"GG latest={version} known={known}")
    if known and _vkey(version) == _vkey(known):
        log("nothing new.")
        return 0

    log(f"new version: {known} → {version}")
    root = extract(fetch(version, url), version)
    decoded = decode(root, version)
    disasm = disassemble(root, decoded, version)
    sonar = decompile_sonar(root, version)
    push_specs_to_research_repo(decoded, disasm, sonar, version)

    edevice_files, spec_hashes = build_index(root, decoded)
    new_files, changed = compare_to_state(edevice_files, spec_hashes, state)
    presets = missing_presets(root)
    changed_code = changed_disassembly(disasm, state)
    changed_sonar_ns = changed_sonar(sonar, state)

    log(f"  new device files : {len(new_files)}")
    log(f"  changed specs    : {len(changed)}")
    log(f"  missing presets  : {len(presets)}")
    log(f"  changed GG code  : {len(changed_code)}")
    log(f"  changed Sonar ns : {len(changed_sonar_ns)}")

    written = add_presets(presets, version) if presets else []
    for n in written:
        log(f"    + {n}")
    issue = open_issue(version, new_files, changed, changed_code, changed_sonar_ns)
    if issue:
        log(f"  issue: {issue}")

    if DRY_RUN:
        log("[dry-run] state not written")
        return 0

    fingerprints = (json.loads((disasm / "fingerprints.json").read_text())
                    if disasm else state.get("disasm_fingerprints", {}))
    sonar_fingerprints = (json.loads((sonar / "fingerprints.json").read_text())
                          if sonar else state.get("sonar_fingerprints", {}))
    save_state(version, edevice_files, spec_hashes, written, new_files, changed, issue,
               fingerprints, changed_code, sonar_fingerprints, changed_sonar_ns)
    commit_state()
    log("done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())