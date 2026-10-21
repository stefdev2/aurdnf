#!/usr/bin/env python3
"""aurdnf - install AUR packages on Fedora by turning the PKGBUILD into an RPM.

Draft. Works for simple PKGBUILDs: http(s) or local sources, a package()
function, optional prepare()/build() (autotools/cmake/meson/cargo, -bin
repackaging).

Dependency names are translated with aurdnf.conf (see that file): Arch names
are looked up in [map] / [map-build], then guessed, and the resulting Fedora
names are written into the RPM, so dnf only ever sees real Fedora packages.

Refused up front (not supported yet): git/svn/hg/bzr sources, pkgver()
functions, split packages, packages that do not support x86_64.
Ignored: .install scripts, pacman hooks, replaces=, optdepends=.

Usage: aurdnf.py <aur-package-name>
Needs: git, rpm-build, bsdtar (libarchive), curl, dnf, sudo
"""
import configparser
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

AUR_GIT = "https://aur.archlinux.org/{}.git"

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


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, text=True, **kw)


def esc(s: str) -> str:
    """Escape text for the spec header: a literal % must be written %%."""
    return s.replace("%", "%%")


def uniq(items) -> list:
    return list(dict.fromkeys(x for x in items if x))


def warn(msg: str) -> None:
    print(f"  warning: {msg}", file=sys.stderr)


# ---------------------------------------------------------------- config ----

def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(delimiters=("=",), interpolation=None,
                                    strict=False)
    cfg.read([p for p in CONFIG_PATHS if p.is_file()])
    return cfg


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


def map_dep(dep: str, build: bool, cfg) -> list:
    """Translate one Arch dependency into a list of Fedora package names.

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
                return []
            out = []
            for t in targets:
                r = resolve(t)
                if r:
                    out.append(r)
                else:
                    warn(f"config maps '{dep}' to '{t}', but Fedora has no such package")
            return out
    for c in ([name + "-devel", name] if build else [name]):
        r = resolve(c)
        if r:
            return [r]
    warn(f"no Fedora package found for '{dep}' (add it to aurdnf.conf)")
    return []


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
        print("aurdnf: cannot build this package yet:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        sys.exit(1)
    if meta.get("install"):
        print(f"aurdnf: warning: install script '{meta['install']}' is ignored "
              "(its pre/post hooks will NOT run)", file=sys.stderr)


# ------------------------------------------------------------------- spec ----

def gen_spec(meta: dict, cfg) -> str:
    name = meta["pkgname"]
    version = meta["pkgver"].replace("-", "_")
    options = meta.get("options", "").split()
    desc = esc((meta.get("pkgdesc") or name).replace("\n", " "))
    licence = " AND ".join(meta.get("license", "").split()) or "Unknown"

    # soname-style entries (libfoo.so=1-64) have no rpm equivalent; rpm adds
    # the real library requirements itself by scanning the binaries
    reqs = uniq(r for d in meta.get("depends", "").split() if ".so" not in d
                for r in map_dep(d, False, cfg))
    breqs = uniq(r for d in meta.get("makedepends", "").split() if ".so" not in d
                 for r in map_dep(d, True, cfg))
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
        f"Release: {meta.get('pkgrel') or 1}",
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


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    pkg = sys.argv[1]
    cfg = load_config()
    work = Path(tempfile.mkdtemp(prefix="aurdnf-"))
    try:
        pkgdir = work / pkg
        run(["git", "clone", "--depth", "1", AUR_GIT.format(pkg), str(pkgdir)])

        print("\n=== PKGBUILD ===")
        print((pkgdir / "PKGBUILD").read_text())
        others = sorted(f.name for f in pkgdir.iterdir()
                        if f.is_file() and f.name != "PKGBUILD")
        if others:
            print("Other files from the AUR repo (copied next to the PKGBUILD): "
                  + ", ".join(others))
        if input("Build and install this? [y/N] ").lower() != "y":
            sys.exit("aborted")

        meta = read_pkgbuild(pkgdir)
        check_supported(meta)

        top = work / "rpmbuild"
        for d in ("SOURCES", "SPECS", "BUILD", "RPMS", "SRPMS"):
            (top / d).mkdir(parents=True)
        shutil.copy(pkgdir / "PKGBUILD", top / "SOURCES" / "PKGBUILD")
        for f in pkgdir.iterdir():  # local sources / patches
            if f.is_file() and f.name != "PKGBUILD":
                shutil.copy(f, top / "SOURCES" / f.name)

        spec = top / "SPECS" / f"{meta['pkgname']}.spec"
        spec.write_text(gen_spec(meta, cfg))

        # downloads are cached so a failed attempt does not re-download
        src_cache = CACHE_ROOT / "sources" / meta["pkgname"]
        src_cache.mkdir(parents=True, exist_ok=True)

        # rpmbuild only *checks* BuildRequires, it does not install them
        run(["sudo", "dnf", "builddep", "-y", str(spec)])
        run(["rpmbuild", "-bb", "--define", f"_topdir {top}",
             "--define", f"_aurdnf_cache {src_cache}", str(spec)])
        rpms = list((top / "RPMS").rglob("*.rpm"))
        if not rpms:
            sys.exit("no RPM produced")

        # keep the RPM outside the temp dir so a failed install can be retried
        keep = CACHE_ROOT / "rpms"
        keep.mkdir(parents=True, exist_ok=True)
        kept = [Path(shutil.copy(r, keep)) for r in rpms]
        try:
            run(["sudo", "dnf", "install", "-y", *map(str, kept)])
        except subprocess.CalledProcessError:
            print("\naurdnf: install failed. The built RPM is kept at:\n  "
                  + "\n  ".join(map(str, kept)), file=sys.stderr)
            sys.exit(1)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
