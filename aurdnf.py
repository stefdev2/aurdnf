#!/usr/bin/env python3
"""aurdnf - install and update AUR packages on Fedora by turning each PKGBUILD
into an RPM that dnf can track and remove.

  aurdnf.py <package> [<package> ...]   install (AUR dependencies are built first)
  aurdnf.py --list                      list the packages aurdnf installed
  aurdnf.py --check [<package> ...]     show which of them have a newer AUR version
  aurdnf.py -u [<package> ...]          update them (all of them if none are named)

Dependency names are translated with aurdnf.conf (see that file): Arch names
are looked up in [map] / [map-build], then guessed against the Fedora repos.
A dependency that Fedora does not have but the AUR does is built and installed
first, as its own RPM. Every PKGBUILD is shown and must be confirmed.

Works for simple PKGBUILDs: http(s) or local sources, a package() function,
optional prepare()/build() (autotools/cmake/meson/cargo, -bin repackaging).
Refused up front (not supported yet): git/svn/hg/bzr sources, pkgver()
functions, split packages, packages that do not support x86_64.
Ignored: .install scripts, pacman hooks, replaces=, optdepends=.

Needs: git, rpm-build, bsdtar (libarchive), curl, dnf, sudo
"""
import argparse
import configparser
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AUR_GIT = "https://aur.archlinux.org/{}.git"
AUR_RPC = "https://aur.archlinux.org/rpc/"
VENDOR = "aurdnf"  # written into every RPM so installed AUR packages can be found

# later files override earlier ones
CONFIG_PATHS = [
    Path(__file__).resolve().with_name("aurdnf.conf"),
    Path.home() / ".config" / "aurdnf" / "aurdnf.conf",
]
CACHE_ROOT = Path.home() / ".cache" / "aurdnf"

FIELDS = ["pkgname", "pkgver", "pkgrel", "pkgdesc", "url", "install"]
ARRAYS = ["arch", "license", "depends", "makedepends", "provides",
          "conflicts", "source", "options"]

# Everything from %prep onwards is static: the PKGBUILD itself is read by bash
# at build time, so nothing PKGBUILD-specific is pasted into the spec.
# NOTE: keep any literal "%" out of the shell code below - rpm would try to
# expand it as a macro. Only %{_sourcedir}, %{_builddir}, %{buildroot} and
# %{_aurdnf_cache} are meant to be expanded by rpm.
SPEC_BODY = r'''%prep

%build
set -e
export startdir="%{_sourcedir}"
export srcdir="%{_builddir}/srcdir"
export pkgdir="%{buildroot}"
mkdir -p "$srcdir"
cd "$srcdir"
source "%{_sourcedir}/PKGBUILD"

# All helper variables are prefixed with _ad_ so they never clobber variables
# the PKGBUILD defines (url, file, ...).
_ad_verify() {
  local f="$1" idx="$2" algo var expected tool actual
  for algo in b2 sha512 sha384 sha256 sha224 sha1 md5; do
    var="${algo}sums[$idx]"
    expected="${!var}"
    if [ -z "$expected" ]; then
      continue
    fi
    if [ "$expected" = "SKIP" ]; then
      echo "aurdnf: checksum skipped by PKGBUILD for $f"
      return 0
    fi
    expected="${expected,,}"
    if [ "$algo" = "b2" ]; then tool=b2sum; else tool="${algo}sum"; fi
    actual="$($tool "$f" | cut -d' ' -f1)"
    if [ "$actual" != "$expected" ]; then
      echo "aurdnf: CHECKSUM MISMATCH for $f ($algo)" >&2
      echo "aurdnf:   expected $expected" >&2
      echo "aurdnf:   actual   $actual" >&2
      return 1
    fi
    echo "aurdnf: checksum OK for $f ($algo)"
    return 0
  done
  echo "aurdnf: WARNING no checksum for $f" >&2
  return 0
}

mkdir -p "%{_aurdnf_cache}"
_ad_i=0
for _ad_s in "${source[@]}"; do
  _ad_url="${_ad_s#*::}"
  _ad_file="${_ad_s/::*/}"
  if [ "$_ad_file" = "$_ad_s" ]; then
    _ad_file="$(basename "$_ad_url")"
  fi
  case "$_ad_url" in
    http://*|https://*|ftp://*)
      if [ -f "%{_aurdnf_cache}/$_ad_file" ]; then
        echo "aurdnf: using cached download of $_ad_file"
        cp "%{_aurdnf_cache}/$_ad_file" "$_ad_file"
      else
        curl -fL --retry 3 -o "$_ad_file" "$_ad_url"
        cp "$_ad_file" "%{_aurdnf_cache}/$_ad_file"
      fi
      ;;
    *)
      cp "%{_sourcedir}/$_ad_url" "$_ad_file"
      ;;
  esac
  if ! _ad_verify "$_ad_file" "$_ad_i"; then
    rm -f "%{_aurdnf_cache}/$_ad_file"
    exit 1
  fi
  _ad_extract=1
  for _ad_n in "${noextract[@]}"; do
    if [ "$_ad_n" = "$_ad_file" ]; then _ad_extract=0; fi
  done
  if [ "$_ad_extract" = 1 ]; then
    bsdtar -xf "$_ad_file" 2>/dev/null || true
  fi
  _ad_i=$((_ad_i + 1))
done

if declare -F prepare >/dev/null; then prepare; fi
if declare -F build >/dev/null; then build; fi

%install
set -e
export startdir="%{_sourcedir}"
export srcdir="%{_builddir}/srcdir"
export pkgdir="%{buildroot}"
cd "$srcdir"
source "%{_sourcedir}/PKGBUILD"
package
(cd "%{buildroot}" && find . \( -type f -o -type l \) -print | LC_ALL=C sort | sed 's|^\.\(.*\)$|"\1"|') > "%{_builddir}/filelist"

%files -f %{_builddir}/filelist
%defattr(-,root,root,-)
'''


class PackageError(Exception):
    """One package could not be built or installed (others may still go on)."""


class AurError(PackageError):
    """The AUR could not be queried."""


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, **kw)


def esc(s: str) -> str:
    """Escape text for the spec header: a literal % must be written %%."""
    return s.replace("%", "%%")


def uniq(items) -> list:
    return list(dict.fromkeys(x for x in items if x))


def warn(msg: str) -> None:
    print(f"  warning: {msg}", file=sys.stderr)


def confirm(prompt: str) -> bool:
    return input(prompt).strip().lower() == "y"


# ---------------------------------------------------------------- config ----

def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(delimiters=("=",), interpolation=None,
                                    strict=False)
    cfg.read([p for p in CONFIG_PATHS if p.is_file()])
    return cfg


# -------------------------------------------------------------- AUR / rpm ----

def aur_info(names: list) -> dict:
    """Ask the AUR for package info. Returns {name: info} for names that exist."""
    found = {}
    names = uniq(names)
    for i in range(0, len(names), 50):
        query = [("v", "5"), ("type", "info")]
        query += [("arg[]", n) for n in names[i:i + 50]]
        url = AUR_RPC + "?" + urllib.parse.urlencode(query)
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                data = json.load(r)
        except (OSError, ValueError) as e:
            raise AurError(f"could not query the AUR: {e}")
        if data.get("type") == "error":
            raise AurError(f"the AUR answered with an error: {data.get('error')}")
        for res in data.get("results", []):
            found[res["Name"]] = res
    return found


def strip_epoch(version: str) -> str:
    return re.sub(r"^\d+:", "", version)


def rpm_installed_vr(name: str):
    """Installed 'version-release' of an rpm package, or None."""
    r = subprocess.run(["rpm", "-q", "--qf", r"%{VERSION}-%{RELEASE}\n", name],
                       capture_output=True, text=True)
    lines = r.stdout.strip().splitlines()
    return lines[0] if r.returncode == 0 and lines else None


def rpm_provides_installed(name: str) -> bool:
    """True if some installed package is called, or provides, `name`."""
    r = subprocess.run(["rpm", "-q", "--whatprovides", name],
                       capture_output=True, text=True)
    return r.returncode == 0


def installed_aur_packages() -> dict:
    """{name: 'version-release'} of every installed rpm that aurdnf built."""
    r = subprocess.run(
        ["rpm", "-qa", "--qf", r"%{NAME}\t%{VERSION}-%{RELEASE}\t%{VENDOR}\n"],
        capture_output=True, text=True)
    out = {}
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[2] == VENDOR:
            out[parts[0]] = parts[1]
    return out


class Session:
    """State shared by everything built in one aurdnf run."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.in_progress = set()
        self.done = set()
        self._aur = {}

    def aur(self, names: list) -> dict:
        """Cached AUR lookup: {name: info} for the names that exist."""
        need = [n for n in uniq(names) if n not in self._aur]
        if need:
            found = aur_info(need)
            for n in need:
                self._aur[n] = found.get(n)
        return {n: self._aur[n] for n in names if self._aur.get(n)}


# ----------------------------------------------------- dependency mapping ----

# dnf prints plain NEVRA lines: name-[epoch:]version-release.arch
NEVRA_RE = re.compile(r"^(?P<name>.+)-(?:\d+:)?[^-]+-[^-]+\.[A-Za-z0-9_]+$")
_resolve_cache: dict = {}


def dnf_package_names(query: str) -> list:
    r = subprocess.run(["dnf", "-q", "repoquery", query],
                       capture_output=True, text=True)
    names = []
    for line in r.stdout.splitlines():
        m = NEVRA_RE.match(line.strip())
        if m:
            names.append(m.group("name"))
    return names


def resolve(name: str):
    """Return the exact Fedora package name for `name`, or None.

    dnf matches names case-insensitively, so 'libsm' finds 'libSM'; the real
    spelling must be used in Requires, because rpm compares case-sensitively.
    """
    if name in _resolve_cache:
        return _resolve_cache[name]
    result = None
    if "(" in name or "/" in name:
        result = name  # capability or file path: cannot be looked up by name
    else:
        names = dnf_package_names(name)
        if name in names:
            result = name
        else:
            folded = [n for n in names if n.lower() == name.lower()]
            if folded:
                result = folded[0]
    _resolve_cache[name] = result
    return result


def dep_name(dep: str) -> str:
    return re.split(r"[<>=:]", dep)[0]  # strip version constraints / descriptions


def map_dep(dep: str, build: bool, cfg):
    """Translate one Arch dependency into Fedora package names.

    Returns (names, unresolved). `unresolved` is True when nothing in the
    config or the Fedora repos matched, so the caller may try the AUR.
    Order: [map-build] (build deps only), [map], then a guess
    (name-devel then name for build deps, name for runtime deps).
    An empty mapping in the config means "drop this dependency".
    """
    name = dep_name(dep)
    key = name.lower()
    for sec in (["map-build", "map"] if build else ["map"]):
        if cfg.has_option(sec, key):
            targets = cfg.get(sec, key).split()
            if not targets:
                print(f"  note: '{dep}' is dropped by the config", file=sys.stderr)
                return [], False
            out = []
            for t in targets:
                r = resolve(t)
                if r:
                    out.append(r)
                else:
                    warn(f"config maps '{dep}' to '{t}', but Fedora has no such package")
            return out, False
    for c in ([name + "-devel", name] if build else [name]):
        r = resolve(c)
        if r:
            return [r], False
    return [], True


def resolve_dependencies(meta: dict, sess: Session):
    """Fedora names for every dependency; AUR-only ones are built first.

    Returns (requires, buildrequires).
    """
    reqs, breqs, pending = [], [], []
    for key, build in (("depends", False), ("makedepends", True)):
        for d in meta.get(key, "").split():
            if ".so" in d:  # soname deps: rpm finds the real ones by scanning
                continue
            names, unresolved = map_dep(d, build, sess.cfg)
            (breqs if build else reqs).extend(names)
            if unresolved:
                pending.append((dep_name(d), build))
    if pending:
        in_aur = sess.aur([n for n, _ in pending])
        for n, build in pending:
            if n not in in_aur:
                warn(f"'{n}' is in neither Fedora nor the AUR; leaving it out "
                     "(map it in aurdnf.conf if it matters)")
                continue
            if n not in sess.done and not rpm_provides_installed(n):
                print(f"  '{n}' is not in Fedora but is in the AUR; "
                      "it will be built first")
                if not install_package(n, sess):
                    raise PackageError(f"dependency '{n}' was not installed")
            (breqs if build else reqs).append(n)
    return uniq(reqs), uniq(breqs)


# --------------------------------------------------------------- PKGBUILD ----

def read_pkgbuild(pkgdir: Path) -> dict:
    """Source the PKGBUILD in bash and dump the fields we care about."""
    script = [
        "source ./PKGBUILD",
        'echo "@@pkgname_count=${#pkgname[@]}"',
        'if declare -F pkgver >/dev/null; then echo "@@has_pkgver=1"; '
        'else echo "@@has_pkgver=0"; fi',
    ]
    for f in FIELDS:
        script.append(f'echo "@@{f}=${{{f}[0]}}"')
    for a in ARRAYS:
        script.append(f'echo "@@{a}=${{{a}[*]}}"')
    out = run(["bash", "-c", "\n".join(script)], cwd=pkgdir,
              capture_output=True).stdout
    meta = {}
    for line in out.splitlines():
        if line.startswith("@@"):
            key, _, val = line[2:].partition("=")
            meta[key] = val
    return meta


def check_supported(meta: dict) -> None:
    problems = []
    if not meta.get("pkgname") or not meta.get("pkgver"):
        problems.append("could not read pkgname/pkgver from the PKGBUILD")
    if meta.get("pkgname_count") != "1":
        problems.append("split packages (several pkgname entries) are not supported yet")
    if meta.get("has_pkgver") == "1":
        problems.append("the PKGBUILD has a pkgver() function (VCS-style version); not supported yet")
    for s in meta.get("source", "").split():
        if re.match(r"^([^:]+::)?(git|svn|hg|bzr)(\+|://)", s):
            problems.append(f"VCS source not supported yet: {s}")
    arch = meta.get("arch", "").split()
    if "x86_64" not in arch and "any" not in arch:
        problems.append(f"package does not list x86_64 or any in arch=({' '.join(arch)})")
    if problems:
        raise PackageError("cannot build this package yet:\n"
                           + "\n".join(f"  - {p}" for p in problems))
    if meta.get("install"):
        print(f"aurdnf: warning: install script '{meta['install']}' is ignored "
              "(its pre/post hooks will NOT run)", file=sys.stderr)


# ------------------------------------------------------------------- spec ----

def rpm_vr(meta: dict):
    """(Version, Release) the RPM will get."""
    return meta["pkgver"].replace("-", "_"), meta.get("pkgrel") or "1"


def gen_spec(meta: dict, cfg, reqs: list, breqs: list) -> str:
    name = meta["pkgname"]
    version, release = rpm_vr(meta)
    options = meta.get("options", "").split()
    desc = esc((meta.get("pkgdesc") or name).replace("\n", " "))
    licence = " AND ".join(meta.get("license", "").split()) or "Unknown"

    # skip soname-style provides (libfoo.so=1-64): meaningless for rpm
    provides = uniq(dep_name(p) for p in meta.get("provides", "").split()
                    if ".so" not in p)
    conflicts = uniq(dep_name(c) for c in meta.get("conflicts", "").split())

    head = [
        "%global debug_package %{nil}",
        "%global _build_id_links none",
    ]
    if "!strip" in options:  # PKGBUILD asks for binaries to be left untouched
        head.append("%global __os_install_post %{nil}")
    # per-package overrides from aurdnf.conf: [<pkgname>] section
    for key, macro in (("requires_exclude", "__requires_exclude"),
                       ("provides_exclude", "__provides_exclude")):
        rx = cfg.get(name, key, fallback="").strip() if cfg.has_section(name) else ""
        if rx:
            head.append(f"%global {macro} {esc(rx)}")
    head += [
        f"Name: {name}",
        f"Version: {version}",
        f"Release: {release}",
        f"Vendor: {VENDOR}",
        f"Summary: {desc}",
        f"License: {esc(licence)}",
    ]
    if meta.get("url"):
        head.append(f"URL: {esc(meta['url'])}")
    head += ["Source0: PKGBUILD", "BuildRequires: bash coreutils curl bsdtar"]
    head += [f"BuildRequires: {b}" for b in breqs]
    head += [f"Requires: {r}" for r in reqs]
    head += [f"Provides: {p}" for p in provides if p != name]
    head += [f"Conflicts: {c}" for c in conflicts if c != name]

    return "\n".join(head) + "\n\n%description\n" + desc + "\n\n" + SPEC_BODY


# ------------------------------------------------------- build and install ----

def fetch_pkgbuild(base: str, dest: Path) -> None:
    run(["git", "clone", "--depth", "1", AUR_GIT.format(base), str(dest)])


def build_rpm(meta: dict, spec_text: str, pkgdir: Path, work: Path) -> list:
    """Build the RPM(s); returns the paths of copies kept in the cache."""
    top = work / "rpmbuild"
    for d in ("SOURCES", "SPECS", "BUILD", "RPMS", "SRPMS"):
        (top / d).mkdir(parents=True)
    shutil.copy(pkgdir / "PKGBUILD", top / "SOURCES" / "PKGBUILD")
    for f in pkgdir.iterdir():  # local sources / patches
        if f.is_file() and f.name != "PKGBUILD":
            shutil.copy(f, top / "SOURCES" / f.name)

    spec = top / "SPECS" / f"{meta['pkgname']}.spec"
    spec.write_text(spec_text)

    # downloads are cached so a failed attempt does not re-download
    src_cache = CACHE_ROOT / "sources" / meta["pkgname"]
    src_cache.mkdir(parents=True, exist_ok=True)

    # rpmbuild only *checks* BuildRequires, it does not install them
    run(["sudo", "dnf", "builddep", "-y", str(spec)])
    run(["rpmbuild", "-bb", "--define", f"_topdir {top}",
         "--define", f"_aurdnf_cache {src_cache}", str(spec)])
    rpms = list((top / "RPMS").rglob("*.rpm"))
    if not rpms:
        raise PackageError("no RPM produced")

    # keep the RPM outside the temp dir so a failed install can be retried
    keep = CACHE_ROOT / "rpms"
    keep.mkdir(parents=True, exist_ok=True)
    return [Path(shutil.copy(r, keep)) for r in rpms]


def install_rpms(name: str, rpms: list, vr: str) -> None:
    paths = [str(r) for r in rpms]
    try:
        if rpm_installed_vr(name) == vr:  # same version again: reinstall it
            try:
                run(["sudo", "dnf", "reinstall", "-y", *paths])
            except subprocess.CalledProcessError:
                run(["sudo", "rpm", "-U", "--replacepkgs", *paths])
        else:
            run(["sudo", "dnf", "install", "-y", *paths])
    except subprocess.CalledProcessError:
        raise PackageError("install failed. The built RPM is kept at:\n  "
                           + "\n  ".join(paths))


def install_package(name: str, sess: Session) -> bool:
    """Build and install one AUR package. False if the user declined it."""
    if name in sess.done:
        return True
    if name in sess.in_progress:
        warn(f"dependency cycle at '{name}'; not building it twice")
        return True
    sess.in_progress.add(name)
    work = Path(tempfile.mkdtemp(prefix="aurdnf-"))
    try:
        info = sess.aur([name]).get(name)
        if not info:
            raise PackageError(f"'{name}' was not found in the AUR")
        base = info.get("PackageBase") or name
        pkgdir = work / base
        fetch_pkgbuild(base, pkgdir)

        print(f"\n=== PKGBUILD: {name} ===")
        print((pkgdir / "PKGBUILD").read_text())
        others = sorted(f.name for f in pkgdir.iterdir()
                        if f.is_file() and f.name != "PKGBUILD")
        if others:
            print("Other files from the AUR repo (copied next to the PKGBUILD): "
                  + ", ".join(others))
        if not confirm(f"Build and install {name}? [y/N] "):
            print(f"aurdnf: skipping {name}")
            return False

        meta = read_pkgbuild(pkgdir)
        check_supported(meta)
        reqs, breqs = resolve_dependencies(meta, sess)
        spec_text = gen_spec(meta, sess.cfg, reqs, breqs)
        rpms = build_rpm(meta, spec_text, pkgdir, work)
        version, release = rpm_vr(meta)
        install_rpms(meta["pkgname"], rpms, f"{version}-{release}")
        sess.done.update({name, meta["pkgname"]})
        return True
    finally:
        sess.in_progress.discard(name)
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- updates ----

def find_outdated(sess: Session, only: list) -> list:
    """[(name, installed 'ver-rel', newest AUR 'ver-rel')] for aurdnf packages."""
    installed = installed_aur_packages()
    if only:
        for n in only:
            if n not in installed:
                warn(f"'{n}' was not installed by aurdnf")
        installed = {n: v for n, v in installed.items() if n in only}
    found = sess.aur(list(installed))
    rows = []
    for n, vr in sorted(installed.items()):
        info = found.get(n)
        if not info:
            warn(f"'{n}' is no longer in the AUR")
            continue
        newest = strip_epoch(info["Version"])
        if newest != vr:
            rows.append((n, vr, newest))
    return rows


def run_packages(names: list, sess: Session) -> int:
    """Install each package; a failure in one does not stop the others."""
    failed, declined = [], []
    for name in uniq(names):
        try:
            if not install_package(name, sess):
                declined.append(name)
        except (PackageError, subprocess.CalledProcessError, OSError) as e:
            print(f"\naurdnf: {name}: {e}", file=sys.stderr)
            failed.append(name)
    ok = [n for n in uniq(names) if n not in failed and n not in declined]
    print()
    if ok:
        print("aurdnf: done: " + ", ".join(ok))
    if declined:
        print("aurdnf: skipped: " + ", ".join(declined))
    if failed:
        print("aurdnf: FAILED: " + ", ".join(failed), file=sys.stderr)
    return 1 if failed else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="aurdnf", description="Install and update AUR packages on Fedora.")
    ap.add_argument("packages", nargs="*", help="AUR package names")
    ap.add_argument("-u", "--update", action="store_true",
                    help="update installed AUR packages (all, or the ones named)")
    ap.add_argument("--check", action="store_true",
                    help="only show which installed AUR packages are outdated")
    ap.add_argument("--list", action="store_true",
                    help="list the packages aurdnf installed")
    args = ap.parse_args(argv)
    sess = Session(load_config())

    try:
        if args.list:
            for n, vr in sorted(installed_aur_packages().items()):
                print(f"{n} {vr}")
            return 0

        if args.update or args.check:
            rows = find_outdated(sess, uniq(args.packages))
            if not rows:
                print("aurdnf: everything is up to date")
                return 0
            for n, old, new in rows:
                print(f"{n}: {old} -> {new}")
            if args.check:
                return 0
            if not confirm(f"\nReview and update {len(rows)} package(s)? [y/N] "):
                return 0
            return run_packages([n for n, _, _ in rows], sess)

        if not args.packages:
            ap.error("name at least one package, or use --list, --check or -u")
        return run_packages(args.packages, sess)
    except AurError as e:
        print(f"aurdnf: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\naurdnf: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
