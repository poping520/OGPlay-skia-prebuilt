#!/usr/bin/env python3
"""Sync, build and verify the pinned Skia checkout used by OGPlay."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from android_skia import (
    ANDROID_ABI, ANDROID_API, ANDROID_DEPENDENCIES, ANDROID_EXCLUDED_MEMBERS,
    ANDROID_LIBRARIES, DEFAULT_ANDROID_SDK, NDK_REVISION, AndroidNdk,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPOSITORY_ROOT / "skia"
LOCAL = REPOSITORY_ROOT / ".local"
SOURCE_URL = "https://skia.googlesource.com/skia"
PINNED_SKIA_COMMIT = "682482c8a67b905f76be24cc3c95215e87dbd3f5"
MILESTONE = "chrome/m157"
MACOS_MIN = "13.3"
ARCHITECTURES = {"arm64": "arm64", "x64": "x86_64"}
ARCHIVE = "libskia.a"
SDK_ARTIFACT = f"sdk/{ARCHIVE}"
SDK_TARGETS = ("skia", "pathops")
# M157 includes the two Mac font helpers via its generic utils list, and
# emits the disabled PDF fallback object. None is part of the SDK API.
EXCLUDED_MEMBERS = (
    "libskia.SkCTFont.o",
    "libskia.SkCTFontCreateExactCopy.o",
    "pdf.SkDocument_PDF_None.o",
)
BUILD_MANIFEST = "ogplay-skia-manifest.json"
# Skia's mac port includes the CG image generator and CG utility APIs.
FRAMEWORKS = ("CoreFoundation", "CoreGraphics", "ImageIO")
BUNDLED_DEPENDENCIES = {
    "//modules/skcms": "skcms",
    "//third_party/freetype2": "freetype",
    "//third_party/libjpeg-turbo": "libjpeg-turbo",
    "//third_party/libpng": "libpng",
    "//third_party/libwebp": "libwebp",
    "//third_party/wuffs": "wuffs",
    "//third_party/zlib": "zlib",
}
FORBIDDEN_SYMBOL = re.compile(
    r"\b(?:SkPDF\w*|SkSVG\w*|GrDirectContext|GrRecordingContext|SkGanesh|"
    r"skgpu::ganesh|skgpu::graphite)::"
    r"|\bSkFontMgr_New_(?!Custom_Empty\b)\w+\("
)


def link_flags(cpu: str) -> list[str]:
    # Upstream adds a macOS 11 triple; Clang uses the last target triple.
    return [f"--target={ARCHITECTURES[cpu]}-apple-macos{MACOS_MIN}"]


def gn_args_for(cpu: str, ndk: Path | str | None = None) -> list[str]:
    args = {
        "extra_cflags_cc": '["-frtti"]',
        "is_component_build": "false",
        "is_debug": "false",
        "is_official_build": "true",
        "skia_enable_fontmgr_custom_empty": "true",
        "skia_gl_standard": '""',
        "target_cpu": json.dumps(cpu),
    }
    if ndk is None:
        target_flags = link_flags(cpu)
        args.update(
            cc='"/usr/bin/clang"',
            cxx='"/usr/bin/clang++"',
            extra_asmflags=json.dumps(target_flags),
            extra_cflags=json.dumps([*target_flags, "-g0"]),
            extra_ldflags=json.dumps(target_flags),
            target_os='"mac"',
        )
    else:
        if cpu != "arm64":
            raise ValueError("Android SDK currently supports only arm64-v8a")
        args.update(
            target_os='"android"',
            ndk=json.dumps(str(ndk)),
            ndk_api=str(ANDROID_API),
            extra_cflags='["-g0"]',
            skia_use_fixed_gamma_text="false",
            skia_use_ndk_images="false",
            skia_use_system_expat="false",
        )
    for feature in (
        "enable_fontmgr_android", "enable_fontmgr_android_ndk",
        "enable_fontmgr_custom_directory", "enable_fontmgr_custom_embedded",
        "enable_fontmgr_empty", "enable_fontmgr_fontations_empty",
        "enable_fontmgr_fontconfig", "enable_fontmgr_FontConfigInterface",
        "enable_fontmgr_win", "enable_fontmgr_win_gdi",
        "enable_ganesh", "enable_graphite", "enable_pdf", "enable_precompile",
        "enable_skottie", "enable_skparagraph", "enable_skshaper",
        "enable_svg", "enable_tools",
        "use_dng_sdk", "use_expat", "use_fontations", "use_fontconfig",
        "use_fonthost_mac", "use_freetype_svg", "use_freetype_woff2",
        "use_freetype_zlib_bundled", "use_gl", "use_harfbuzz", "use_icu",
        "use_jpeg_gainmaps", "use_libwebp_encode", "use_metal",
        "use_partition_alloc", "use_perfetto", "use_piex",
        "use_vulkan", "use_xps",
        "use_system_freetype2", "use_system_libjpeg_turbo",
        "use_system_libpng", "use_system_libwebp", "use_system_zlib",
    ):
        args[f"skia_{feature}"] = "false"
    for feature in (
        "freetype", "freetype_zlib", "libjpeg_turbo_decode",
        "libjpeg_turbo_encode", "libpng_decode", "libpng_encode",
        "libwebp_decode", "wuffs", "zlib",
    ):
        args[f"skia_use_{feature}"] = "true"
    return [f"{name}={value}" for name, value in sorted(args.items())]


def run(command, cwd: Path, *, env=None) -> None:
    print(f"[{cwd}] {shlex.join(str(arg) for arg in command)}", flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def capture(command, cwd: Path = REPOSITORY_ROOT) -> str:
    return subprocess.check_output(command, cwd=cwd, encoding="utf-8").strip()


def preflight(*, build: bool = False, link: bool = False) -> None:
    if sys.platform != "darwin":
        raise RuntimeError("the macOS SDK must be built and verified on macOS")
    tools = ["lipo", "otool", "nm", "ar"]
    if build:
        tools += ["git", "ninja"]
    if build or link:
        tools += ["xcrun"]
    missing = [name for name in tools if shutil.which(name) is None]
    if missing:
        raise RuntimeError(f"missing required tools: {', '.join(missing)}")
    if build or link:
        sdk = Path(capture(["xcrun", "--sdk", "macosx", "--show-sdk-path"]))
        if not sdk.is_dir() or not all(Path(f"/usr/bin/{name}").is_file()
                                       for name in ("clang", "clang++")):
            raise RuntimeError("system Clang and the macOS SDK are required")


def verify_checkout(source: Path) -> None:
    if not (source / ".git").exists():
        raise RuntimeError(f"Skia checkout is missing: {source}; run sync")
    commit = capture(["git", "rev-parse", "HEAD"], source)
    if commit != PINNED_SKIA_COMMIT:
        raise RuntimeError(f"Skia checkout is {commit}; expected {PINNED_SKIA_COMMIT}")
    if capture(["git", "status", "--porcelain", "--untracked-files=no"], source):
        raise RuntimeError(f"Skia checkout has modified tracked files: {source}")


def sync(source: Path) -> None:
    if not source.exists():
        source.mkdir(parents=True)
        run(["git", "init", "-b", "main"], source)
    if (source / ".git").is_dir() and not (source / "DEPS").exists():
        head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=source,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if head.returncode != 0:
            run(["git", "fetch", "--depth=1", SOURCE_URL, PINNED_SKIA_COMMIT], source)
            run(["git", "checkout", "--detach", PINNED_SKIA_COMMIT], source)
    verify_checkout(source)
    # git-sync-deps also calls bin/fetch-gn. The desktop build needs no emsdk.
    env = {**os.environ, "GIT_SYNC_DEPS_SKIP_EMSDK": "1", "TMPDIR": str(LOCAL)}
    env.pop("GIT_SYNC_DEPS_PATH", None)
    LOCAL.mkdir(exist_ok=True)
    run([sys.executable, "tools/git-sync-deps"], source, env=env)
    verify_checkout(source)


def configure(source: Path, output: Path, gn_args: list[str]) -> None:
    gn = source / "bin/gn"
    if not gn.is_file():
        raise RuntimeError("Skia GN is missing; run sync (which calls bin/fetch-gn)")
    output.mkdir(parents=True, exist_ok=True)
    (output / "args.gn").write_text("\n".join(gn_args) + "\n", encoding="utf-8")
    (output / BUILD_MANIFEST).unlink(missing_ok=True)
    run([gn, "gen", "--fail-on-unused-args", output], source)


def check_config(output: Path, gn_args: list[str]) -> None:
    args_file = output / "args.gn"
    if not args_file.is_file() or args_file.read_text(encoding="utf-8").splitlines() != gn_args:
        raise RuntimeError(f"GN args differ or are missing at {output}; run configure")


def file_record(path: Path, root: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "path": path.relative_to(root).as_posix(),
        "size": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def write_manifest(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def archive_members(archive: Path, ar: Path | str) -> list[str]:
    index_names = {"__.SYMDEF", "__.SYMDEF SORTED", "__.SYMDEF_64", "__.SYMDEF_64 SORTED"}
    return [name for name in capture([ar, "-t", archive]).splitlines() if name not in index_names]


def finalize_archive(source: Path, output: Path, ndk: AndroidNdk | None) -> None:
    upstream = output / ARCHIVE
    ar = ndk.bin / "llvm-ar" if ndk else "ar"
    excluded = ANDROID_EXCLUDED_MEMBERS if ndk else EXCLUDED_MEMBERS
    counts = Counter(archive_members(upstream, ar))
    if any(counts[name] != 1 for name in excluded):
        raise RuntimeError(f"expected archive members to remove from {upstream}: {excluded}")
    # Upstream's CPU-only skia target omits the separate PathOps source set.
    desc = json.loads(capture(
        [source / "bin/gn", "desc", output, "//:pathops", "source_outputs", "--format=json"], source
    ))
    pathops_objects = [
        output / name for names in desc["//:pathops"]["source_outputs"].values()
        for name in names if name.endswith(".o")
    ]
    if not pathops_objects:
        raise RuntimeError("GN returned no PathOps object files")
    artifact = output / SDK_ARTIFACT
    artifact.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=artifact.parent) as scratch:
        candidate = Path(scratch) / ARCHIVE
        shutil.copyfile(upstream, candidate)
        env = {**os.environ, "ZERO_AR_DATE": "1"}
        run([ar, "-d", candidate, *excluded], output, env=env)
        run([ar, "-r", candidate, *pathops_objects], output, env=env)
        candidate.replace(artifact)


def verify_archive(archive: Path, cpu: str, ndk: AndroidNdk | None = None) -> None:
    ar = ndk.bin / "llvm-ar" if ndk else "ar"
    members = archive_members(archive, ar)
    if not members or any(not member.endswith(".o") for member in members):
        raise RuntimeError(f"expected a nonempty object archive: {archive}")
    if ndk:
        ndk.verify_objects(archive, len(members))
        check_symbols(archive, ndk.bin / "llvm-nm", r"^(?:egl|gl)[A-Z]\w+")
        print(f"{archive}: {len(members)} ELF AArch64 objects; symbol check passed")
        return

    arch = ARCHITECTURES[cpu]
    lipo_info = capture(["lipo", "-info", archive])
    if not lipo_info.startswith("Non-fat file:") or not lipo_info.endswith(f"is architecture: {arch}"):
        raise RuntimeError(f"expected thin {arch} archive: {lipo_info}")
    load_commands = capture(["otool", "-v", "-l", archive])
    if "segname __DWARF" in load_commands:
        raise RuntimeError(f"unexpected debug sections in {archive}")
    versions = re.findall(
        r"cmd LC_BUILD_VERSION\n\s*cmdsize \d+\n\s*platform (\w+)\n\s*minos (\S+)",
        load_commands,
    )
    if Counter(versions) != {("MACOS", MACOS_MIN): len(members)}:
        raise RuntimeError(
            f"{archive}: expected {len(members)} macOS {MACOS_MIN} objects; "
            f"otool reported {dict(Counter(versions))}"
        )
    check_symbols(archive, "nm", r"^_(?:CT[A-Z]|kCT[A-Z]|CGL[A-Z]|gl[A-Z])\w+")
    print(f"{lipo_info}; {len(members)} objects, minos {MACOS_MIN}; symbol check passed")


def check_symbols(archive: Path, nm: Path | str, forbidden_imports: str) -> None:
    symbols = capture([nm, "--quiet", "-gj", "--defined-only", "-C", archive])
    if bad := FORBIDDEN_SYMBOL.search(symbols):
        raise RuntimeError(f"disabled feature found in {archive}: {bad.group(0)}")
    if "SkFontMgr_New_Custom_Empty(" not in symbols:
        raise RuntimeError(f"custom empty font manager is missing from {archive}")
    # Demangling accounts for the extra leading underscore in Mach-O.
    definitions = set(symbols.splitlines())
    if "typeinfo for SkWStream" not in definitions:
        raise RuntimeError(f"SkWStream RTTI definition is missing from {archive}")
    pathops = {
        "Op(SkPath const&, SkPath const&, SkPathOp)",
        "Simplify(SkPath const&)",
        "AsWinding(SkPath const&)",
        "SkOpBuilder::add(SkPath const&, SkPathOp)",
        "SkOpBuilder::resolve()",
    }
    if missing := pathops - definitions:
        raise RuntimeError(f"PathOps definitions are missing from {archive}: {', '.join(sorted(missing))}")
    undefined = capture([nm, "--quiet", "-uj", archive])
    if bad := re.search(forbidden_imports, undefined, re.M):
        raise RuntimeError(f"unexpected font or GL import in {archive}: {bad.group(0)}")


def verify_full_link(archive: Path, cpu: str, ndk: AndroidNdk | None) -> None:
    with tempfile.TemporaryDirectory(dir=archive.parent) as scratch:
        binary = Path(scratch) / "link-check"
        if ndk:
            command = [
                ndk.clang, "-x", "c++", "-", "-x", "none",
                "-Wl,--whole-archive", archive, "-Wl,--no-whole-archive",
                *(f"-l{name}" for name in ANDROID_LIBRARIES),
            ]
        else:
            command = [
                "/usr/bin/clang++", *link_flags(cpu), "-x", "c++", "-",
                "-Xlinker", "-force_load", "-Xlinker", archive,
            ]
            for framework in FRAMEWORKS:
                command += ["-framework", framework]
        subprocess.run([*command, "-o", binary], check=True,
                       input="int main() { return 0; }\n", encoding="utf-8")
        if ndk:
            ndk.verify_executable(binary)
    print("whole-archive link passed" + (f" (Android API {ANDROID_API})" if ndk else ""))


def consumer_defines(source: Path, output: Path) -> list[str]:
    # This upstream target consumes //:skia. Its Vulkan header config is local
    # to that target. NDEBUG is translated to the equivalent Skia-only mode.
    desc = json.loads(capture(
        [source / "bin/gn", "desc", output, "//:public_headers_warnings_check",
         "defines", "--format=json"], source
    ))
    (target,) = desc.values()
    defines = set(target["defines"])
    if "NDEBUG" not in defines or "SK_FONTMGR_FREETYPE_EMPTY_AVAILABLE" not in defines:
        raise RuntimeError(f"unexpected GN consumer defines: {sorted(defines)}")
    return sorted({"SK_RELEASE"} | {
        value for value in defines
        if value.startswith("SK_") and value != "SK_USE_INTERNAL_VULKAN_HEADERS"
    })


def bundled_dependencies(platform_name: str) -> dict[str, str]:
    return ({**BUNDLED_DEPENDENCIES, **ANDROID_DEPENDENCIES}
            if platform_name == "android" else BUNDLED_DEPENDENCIES)


def platform_fields(cpu: str, platform_name: str = "macos") -> dict:
    fields = {
        "schema_version": 1,
        "skia_commit": PINNED_SKIA_COMMIT,
        "milestone": MILESTONE,
        "platform": platform_name,
        "target_cpu": cpu,
        "configuration": "release",
    }
    if platform_name == "android":
        return {
            **fields,
            "android_abi": ANDROID_ABI,
            "minimum_android_api": ANDROID_API,
            "ndk_revision": NDK_REVISION,
            "gn_args": gn_args_for(cpu, "<NDK>"),
            "system_frameworks": [],
            "system_libraries": list(ANDROID_LIBRARIES),
            "cxx_runtime": "libc++_shared.so",
            "bundled_dependencies": sorted(bundled_dependencies(platform_name).values()),
            "excluded_archive_members": list(ANDROID_EXCLUDED_MEMBERS),
        }
    return {
        **fields,
        "minimum_macos": MACOS_MIN,
        "gn_args": gn_args_for(cpu),
        "system_frameworks": list(FRAMEWORKS),
        "system_libraries": [],
        "bundled_dependencies": sorted(BUNDLED_DEPENDENCIES.values()),
        "excluded_archive_members": list(EXCLUDED_MEMBERS),
    }


def verify_build(source: Path, output: Path, cpu: str, ndk: AndroidNdk | None) -> None:
    platform_name = "android" if ndk else "macos"
    bundled = {
        label.split(":", 1)[0] for target in SDK_TARGETS
        for label in capture([source / "bin/gn", "desc", output, f"//:{target}", "deps", "--all"],
                             source).splitlines()
        if label.startswith(("//modules/", "//third_party/"))
    }
    if bundled != bundled_dependencies(platform_name).keys():
        raise RuntimeError(f"bundled dependency graph differs from license inventory: {sorted(bundled)}")
    archive = output / SDK_ARTIFACT
    verify_archive(archive, cpu, ndk)
    verify_full_link(archive, cpu, ndk)
    manifest = {
        **platform_fields(cpu, platform_name),
        "consumer_defines": consumer_defines(source, output),
        "targets": [f"//:{target}" for target in SDK_TARGETS],
        "artifacts": [SDK_ARTIFACT],
        "files": [file_record(archive, output)],
    }
    manifest_path = output / BUILD_MANIFEST
    if manifest_path.exists() and json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
        raise RuntimeError(f"build output differs from {manifest_path}; run build")
    write_manifest(manifest_path, manifest)


def self_test() -> None:
    arm = gn_args_for("arm64")
    x64 = gn_args_for("x64")
    assert arm == sorted(arm)
    for cpu, config in (("arm64", arm), ("x64", x64)):
        assert f'target_cpu="{cpu}"' in config
        assert all(link_flags(cpu)[0] in arg for arg in config
                   if arg.startswith(("extra_asmflags=", "extra_cflags=", "extra_ldflags=")))
    cpu_specific = ("target_cpu=", "extra_")
    assert [arg for arg in arm if not arg.startswith(cpu_specific)] == [
        arg for arg in x64 if not arg.startswith(cpu_specific)
    ]
    assert 'skia_enable_fontmgr_custom_empty=true' in arm
    assert all(arg.endswith("=false") for arg in arm if arg.startswith("skia_use_system_"))
    assert FORBIDDEN_SYMBOL.search("SkPDF::MakeDocument()")
    assert FORBIDDEN_SYMBOL.search("SkFontMgr_New_CoreText()")
    assert not FORBIDDEN_SYMBOL.search("SkFontMgr_New_Custom_Empty()")
    android = gn_args_for("arm64", "<NDK>")
    for config in (arm, x64, android):
        assert [arg for arg in config if "-frtti" in arg] == ['extra_cflags_cc=["-frtti"]']
    assert 'target_os="android"' in android and "ndk_api=23" in android
    assert "skia_use_system_expat=false" in android and "skia_use_fixed_gamma_text=false" in android
    assert not any("apple-macos" in arg for arg in android)
    assert re.fullmatch("[0-9a-f]{40}", PINNED_SKIA_COMMIT)
    print("Skia build driver self-test passed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="all",
                        choices=("sync", "configure", "build", "verify", "all", "print-config"))
    parser.add_argument("--platform", choices=("macos", "android"), default="macos")
    parser.add_argument("--cpu", choices=ARCHITECTURES)
    parser.add_argument("--android-sdk", type=Path, default=DEFAULT_ANDROID_SDK)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--jobs", type=int)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if args.jobs is not None and args.jobs < 1:
        parser.error("--jobs must be positive")
    android = args.platform == "android"
    cpu = args.cpu or ("arm64" if android or platform.machine() == "arm64" else "x64")
    ndk = AndroidNdk.from_sdk(args.android_sdk) if android else None
    gn_args = gn_args_for(cpu, ndk.root if ndk else None)
    if args.action == "print-config":
        print("\n".join(gn_args))
        return

    if ndk:
        if sys.platform != "darwin":
            raise RuntimeError("Android SDK builds currently use the macOS NDK host tools")
        for name in ("git", "ninja"):
            if not shutil.which(name):
                raise RuntimeError(f"missing required tool: {name}")
    else:
        preflight(build=args.action != "verify", link=True)
    source = args.source.resolve()
    output = (args.output or LOCAL / f"{args.platform}-{cpu}").resolve()
    if args.action not in ("all", "sync"):
        verify_checkout(source)
    steps = ("sync", "configure", "build", "verify") if args.action == "all" else (args.action,)
    for step in steps:
        start = time.monotonic()
        if step == "sync":
            sync(source)
        elif step == "configure":
            configure(source, output, gn_args)
        else:
            check_config(output, gn_args)
            if step == "build":
                (output / BUILD_MANIFEST).unlink(missing_ok=True)
                command = ["ninja", "-C", output, *SDK_TARGETS]
                if args.jobs:
                    command += ["-j", str(args.jobs)]
                # Apple libtool and ar honor this for archive member timestamps.
                run(command, source, env={**os.environ, "ZERO_AR_DATE": "1"})
                finalize_archive(source, output, ndk)
            else:
                verify_build(source, output, cpu, ndk)
        print(f"{step}: {time.monotonic() - start:.2f}s", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError) as error:
        sys.exit(f"Skia build failed: {error}")
