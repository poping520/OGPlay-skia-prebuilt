#!/usr/bin/env python3
"""Package and verify the relocatable Skia SDK for OGPlay."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time

from android_skia import DEFAULT_ANDROID_SDK, AndroidNdk
from build_skia import (
    ARCHITECTURES, ARCHIVE, BUILD_MANIFEST, DEFAULT_SOURCE, LOCAL, MILESTONE,
    PINNED_SKIA_COMMIT, REPOSITORY_ROOT, SDK_ARTIFACT, file_record, platform_fields,
    bundled_dependencies, preflight, verify_archive, verify_checkout, write_manifest,
)


SDK_MANIFEST = "manifest.json"
DEFAULT_DESTINATION = LOCAL / "skia-sdk"
DEFAULT_HEADERS = REPOSITORY_ROOT / "include"
DEFAULT_LICENSES = REPOSITORY_ROOT / "licenses"
LIBRARY = f"lib/{ARCHIVE}"
HEADER_GROUPS = ("include/core", "include/cpu", "include/effects", "include/pathops", "include/utils")
HEADER_ENTRIES = (
    "include/codec/SkCodec.h",
    "include/codec/SkCodecAnimation.h",
    "include/codec/SkEncodedImageFormat.h",
    "include/codec/SkEncodedOrigin.h",
    "include/codec/SkPixmapUtils.h",
    "include/codec/SkBmpDecoder.h",
    "include/codec/SkGifDecoder.h",
    "include/codec/SkIcoDecoder.h",
    "include/codec/SkJpegDecoder.h",
    "include/codec/SkPngDecoder.h",
    "include/codec/SkWbmpDecoder.h",
    "include/codec/SkWebpDecoder.h",
    "include/encode/SkEncoder.h",
    "include/encode/SkICC.h",
    "include/encode/SkJpegEncoder.h",
    "include/encode/SkPngEncoder.h",
    "include/ports/SkCFObject.h",
    "include/ports/SkFontMgr_empty.h",
    "include/ports/SkFontScanner_FreeType.h",
    "include/ports/SkImageGeneratorCG.h",
)
QUOTED_INCLUDE = re.compile(r'^\s*#\s*include\s+"([^"]+)"', re.M)
EXTERNALS = "third_party/externals"
LICENSE_SOURCES = {
    "Skia.txt": "LICENSE",
    "third-party/skcms/LICENSE": "modules/skcms/LICENSE",
    "third-party/freetype/FTL.TXT": f"{EXTERNALS}/freetype/docs/FTL.TXT",
    "third-party/libpng/LICENSE": f"{EXTERNALS}/libpng/LICENSE",
    "third-party/libjpeg-turbo/LICENSE.md": f"{EXTERNALS}/libjpeg-turbo/LICENSE.md",
    "third-party/libjpeg-turbo/README.ijg": f"{EXTERNALS}/libjpeg-turbo/README.ijg",
    "third-party/libwebp/COPYING": f"{EXTERNALS}/libwebp/COPYING",
    "third-party/libwebp/PATENTS": f"{EXTERNALS}/libwebp/PATENTS",
    "third-party/wuffs/LICENSE": f"{EXTERNALS}/wuffs/LICENSE",
    "third-party/zlib/LICENSE": f"{EXTERNALS}/zlib/LICENSE",
}
EMBEDDED_NOTICES = {
    "third-party/freetype/fthash.txt": (f"{EXTERNALS}/freetype/src/base/fthash.c",
                                        "Permission is hereby granted"),
    "third-party/freetype/harfbuzz-headers.txt": (
        f"{EXTERNALS}/freetype/src/autofit/ft-hb-types.h", "Permission is hereby granted"),
    "third-party/freetype/harfbuzz-scripts.txt": (
        f"{EXTERNALS}/freetype/src/autofit/hb-script-list.h", "This is part of HarfBuzz"),
    "third-party/wuffs/source-notice.txt": (
        f"{EXTERNALS}/wuffs/release/c/wuffs-v0.3.c", "// Copyright 2017 The Wuffs Authors."),
    "third-party/zlib/zlib-h-notice.txt": (f"{EXTERNALS}/zlib/zlib.h", "Jean-loup Gailly"),
}
CHROMIUM_LICENSE = "third-party/zlib/Chromium.txt"
LICENSE_FILES = set(LICENSE_SOURCES) | set(EMBEDDED_NOTICES) | {CHROMIUM_LICENSE}
ANDROID_LICENSE_FILES = {
    "third-party/expat/COPYING",
    "third-party/ndk-cpu-features/LICENSE-BSD.txt",
    "third-party/ndk-cpu-features/LICENSE-APACHE.txt",
    "third-party/ndk-cpu-features/NOTICE",
}
REQUIRED_DEFINES = {
    "SK_CODEC_DECODES_GIF", "SK_CODEC_DECODES_JPEG", "SK_CODEC_DECODES_PNG_WITH_LIBPNG",
    "SK_CODEC_DECODES_WEBP", "SK_CODEC_ENCODES_JPEG", "SK_CODEC_ENCODES_PNG_WITH_LIBPNG",
    "SK_FONTMGR_FREETYPE_EMPTY_AVAILABLE", "SK_RELEASE", "SK_TYPEFACE_FACTORY_FREETYPE",
}


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"manifest root must be an object: {path}")
    return value


def manifest_files(root: Path) -> list[dict]:
    records = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"SDK contains a symlink: {path}")
        if path.is_file() and path != root / SDK_MANIFEST and path.name != ".DS_Store":
            records.append(file_record(path, root))
    return records


def verify_files(root: Path, expected: object) -> set[str]:
    records = manifest_files(root)
    if not records or records != expected:
        raise RuntimeError(f"files differ from manifest at {root} (path, size or SHA-256)")
    return {record["path"] for record in records}


@contextmanager
def staging(destination: Path):
    if destination.exists() or destination.is_symlink():
        raise RuntimeError(f"destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".staging-") as scratch:
        staged = Path(scratch)
        yield staged
        if destination.exists() or destination.is_symlink():
            raise RuntimeError(f"destination already exists: {destination}")
        staged.chmod(0o755)
        staged.rename(destination)


def header_paths(source: Path) -> set[Path]:
    pending = [source / entry for entry in HEADER_ENTRIES]
    for directory in HEADER_GROUPS:
        headers = list((source / directory).rglob("*.h"))
        if not headers:
            raise RuntimeError(f"missing headers in {source / directory}")
        pending.extend(headers)
    found = set()
    while pending:
        path = pending.pop()
        if path in found:
            continue
        found.add(path)
        for include in QUOTED_INCLUDE.findall(path.read_text(encoding="utf-8")):
            if include.startswith(("include/", "modules/")):
                pending.append(source / include)
            elif (path.parent / include).is_file():
                pending.append(path.parent / include)
    return found


def copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def shared_fields(kind: str, platform_name: str = "macos") -> dict:
    fields = {
        "schema_version": 1,
        "kind": kind,
        "skia_commit": PINNED_SKIA_COMMIT,
        "milestone": MILESTONE,
    }
    if kind == "licenses":
        fields["bundled_dependencies"] = sorted(bundled_dependencies(platform_name).values())
    return fields


def verify_shared(root: Path, manifest: dict, kind: str, platform_name: str = "macos") -> set[str]:
    if any(manifest.get(key) != value for key, value in shared_fields(kind, platform_name).items()):
        raise RuntimeError(f"unexpected {kind} manifest at {root}")
    return verify_files(root, manifest.get("files"))


def verify_headers(root: Path) -> None:
    paths = verify_shared(root, read_json(root / SDK_MANIFEST), "headers")
    required = set(HEADER_ENTRIES) | {
        "include/core/SkCanvas.h", "include/config/SkUserConfig.h",
        "modules/skcms/skcms.h", "modules/skcms/src/skcms_public.h",
    }
    if not required <= paths or any(
        not path.endswith(".h")
        or not path.startswith(("include/", "modules/skcms/"))
        or path.startswith(("include/gpu/", "include/svg/", "include/docs/"))
        for path in paths
    ):
        raise RuntimeError(f"unexpected public headers at {root}")


def verify_licenses(root: Path) -> set[str]:
    manifest = read_json(root / SDK_MANIFEST)
    android = manifest.get("bundled_dependencies") == sorted(bundled_dependencies("android").values())
    expected = LICENSE_FILES | ANDROID_LICENSE_FILES if android else LICENSE_FILES
    if verify_shared(root, manifest, "licenses", "android" if android else "macos") != expected:
        raise RuntimeError(f"incomplete or unexpected third-party notices at {root}")
    return set(manifest["bundled_dependencies"])


def package_headers(source: Path, destination: Path) -> None:
    verify_checkout(source)
    with staging(destination) as staged:
        for path in sorted(header_paths(source)):
            copy_file(path, staged / path.relative_to(source))
        write_manifest(staged / SDK_MANIFEST, {
            **shared_fields("headers"), "files": manifest_files(staged),
        })
        verify_headers(staged)


def source_notice(source: Path, marker: str) -> str:
    text = source.read_text(encoding="utf-8")
    if marker.startswith("//"):
        lines = text[text.index(marker):].splitlines()
        return "\n".join(lines[:lines.index("")]) + "\n"
    (notice,) = [block for block in re.findall(r"/\*.*?\*/", text, re.S) if marker in block]
    return notice + "\n"


def package_licenses(source: Path, destination: Path, ndk: AndroidNdk | None = None) -> None:
    verify_checkout(source)
    with staging(destination) as staged:
        for target, name in LICENSE_SOURCES.items():
            copy_file(source / name, staged / target)
        copy_file(REPOSITORY_ROOT / "tools/notices/Chromium.txt", staged / CHROMIUM_LICENSE)
        for target, (name, marker) in EMBEDDED_NOTICES.items():
            notice = staged / target
            notice.parent.mkdir(parents=True, exist_ok=True)
            notice.write_text(source_notice(source / name, marker), encoding="utf-8")
        if ndk:
            cpu_licenses = staged / "third-party/ndk-cpu-features"
            copy_file(source / f"{EXTERNALS}/expat/expat/COPYING", staged / "third-party/expat/COPYING")
            copy_file(ndk.cpu_features / "NOTICE", cpu_licenses / "NOTICE")
            copy_file(source / LICENSE_SOURCES["third-party/wuffs/LICENSE"], cpu_licenses / "LICENSE-APACHE.txt")
            (cpu_licenses / "LICENSE-BSD.txt").write_text(
                source_notice(ndk.cpu_features / "cpu-features.c", "Redistribution and use"),
                encoding="utf-8",
            )
        write_manifest(staged / SDK_MANIFEST, {
            **shared_fields("licenses", "android" if ndk else "macos"), "files": manifest_files(staged),
        })
        verify_licenses(staged)


def validate_platform(manifest: dict) -> tuple[str, str]:
    cpu = manifest.get("target_cpu")
    platform_name = manifest.get("platform")
    if (platform_name not in ("macos", "android") or not isinstance(cpu, str)
            or cpu not in ARCHITECTURES or (platform_name == "android" and cpu != "arm64")
            or any(manifest.get(key) != value for key, value in platform_fields(cpu, platform_name).items())):
        raise RuntimeError("unexpected Skia platform manifest")
    defines = manifest.get("consumer_defines")
    if (
        not isinstance(defines, list)
        or not all(isinstance(define, str) and re.fullmatch(r"SK_\w+(?:=\w+)?", define)
                   for define in defines)
        or defines != sorted(set(defines))
        or not REQUIRED_DEFINES <= set(defines)
        or set(defines) & {"SK_DEBUG", "SK_GANESH", "SK_GRAPHITE", "SK_GL", "SK_SUPPORT_PDF"}
    ):
        raise RuntimeError("invalid Skia consumer defines")
    return platform_name, cpu


def verify_sdk(sdk: Path, android_sdk: Path = DEFAULT_ANDROID_SDK) -> dict:
    manifest = read_json(sdk / SDK_MANIFEST)
    platform_name, cpu = validate_platform(manifest)
    if (manifest.get("link_order") != [LIBRARY]
            or manifest.get("shared_headers") != "include"
            or manifest.get("shared_licenses") != "licenses"):
        raise RuntimeError("invalid Skia SDK layout or link order")
    if verify_files(sdk, manifest.get("files")) != {LIBRARY}:
        raise RuntimeError(f"platform SDK must only contain {LIBRARY}: {sdk}")
    verify_headers(sdk.parent.parent / "include")
    licensed = verify_licenses(sdk.parent.parent / "licenses")
    if not set(manifest["bundled_dependencies"]) <= licensed:
        raise RuntimeError(f"missing licenses for bundled dependencies in {sdk}")
    ndk = AndroidNdk.from_sdk(android_sdk) if platform_name == "android" else None
    if not ndk:
        preflight()
    verify_archive(sdk / LIBRARY, cpu, ndk)
    return manifest


def package_sdk(build: Path, root: Path, android_sdk: Path = DEFAULT_ANDROID_SDK) -> Path:
    manifest = read_json(build / BUILD_MANIFEST)
    platform_name, cpu = validate_platform(manifest)
    archive = build / SDK_ARTIFACT
    if manifest.get("artifacts") != [SDK_ARTIFACT] or manifest.get("files") != [file_record(archive, build)]:
        raise RuntimeError(f"archive differs from verified build manifest at {build}")
    destination = root / f"{platform_name}-{cpu}" / "release"
    with staging(destination) as staged:
        copy_file(archive, staged / LIBRARY)
        write_manifest(staged / SDK_MANIFEST, {
            **platform_fields(cpu, platform_name),
            "consumer_defines": manifest["consumer_defines"],
            "link_order": [LIBRARY],
            "shared_headers": "include",
            "shared_licenses": "licenses",
            "files": manifest_files(staged),
        })
        verify_sdk(staged, android_sdk)
    return destination


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="ogplay-skia-sdk-") as scratch:
        root = Path(scratch)
        payload = root / "sample"
        with staging(payload) as staged:
            (staged / "data").write_bytes(b"abc")
            records = manifest_files(staged)
        assert verify_files(payload, records) == {"data"}
        (payload / "data").write_bytes(b"abd")
        try:
            verify_files(payload, records)
        except RuntimeError as error:
            assert "SHA-256" in str(error)
        else:
            raise AssertionError("corrupted payload unexpectedly verified")
        failed = root / "failed"
        try:
            with staging(failed):
                raise RuntimeError("test rollback")
        except RuntimeError:
            assert not failed.exists()
    manifest = {**platform_fields("arm64"), "consumer_defines": sorted(REQUIRED_DEFINES)}
    assert validate_platform(manifest) == ("macos", "arm64")
    android = {**platform_fields("arm64", "android"), "consumer_defines": sorted(REQUIRED_DEFINES)}
    assert validate_platform(android) == ("android", "arm64")
    print("Skia SDK packager self-test passed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="package",
                        choices=("package-headers", "package-licenses", "package",
                                 "verify", "verify-headers", "verify-licenses"))
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument("--build", type=Path)
    parser.add_argument("--sdk", type=Path)
    parser.add_argument("--headers", type=Path, default=DEFAULT_HEADERS)
    parser.add_argument("--licenses", type=Path, default=DEFAULT_LICENSES)
    parser.add_argument("--android-sdk", type=Path, help="include Android notices when packaging licenses")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return

    start = time.monotonic()
    if args.action == "package":
        if args.build is None:
            parser.error("package requires --build")
        path = package_sdk(args.build.resolve(), args.destination.resolve(), args.android_sdk or DEFAULT_ANDROID_SDK)
    elif args.action == "verify":
        if args.sdk is None:
            parser.error("verify requires --sdk")
        path = args.sdk.resolve()
        verify_sdk(path, args.android_sdk or DEFAULT_ANDROID_SDK)
    elif args.action in ("package-headers", "verify-headers"):
        path = args.headers.resolve()
        if args.action == "package-headers":
            package_headers(args.source.resolve(), path)
        else:
            verify_headers(path)
    else:
        path = args.licenses.resolve()
        if args.action == "package-licenses":
            ndk = AndroidNdk.from_sdk(args.android_sdk) if args.android_sdk else None
            package_licenses(args.source.resolve(), path, ndk)
        else:
            verify_licenses(path)
    print(f"{args.action}: {path} ({time.monotonic() - start:.2f}s)")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        sys.exit(f"Skia SDK packaging failed: {error}")
