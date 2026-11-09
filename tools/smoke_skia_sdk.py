#!/usr/bin/env python3
"""Compile and run a C++20 consumer against a packaged Skia SDK."""

from __future__ import annotations

import argparse
from base64 import b64decode
from pathlib import Path
import platform
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib

from android_skia import ANDROID_ABI, ANDROID_API, DEFAULT_ANDROID_SDK, AndroidNdk
from build_skia import LOCAL, REPOSITORY_ROOT, capture, link_flags, run
from package_skia_sdk import verify_sdk


ROM_FONT_PATH = Path("build/release/data/android/19/fonts")
ROM_FONTS = (
    REPOSITORY_ROOT.parent / "OGPlay" / ROM_FONT_PATH,
    REPOSITORY_ROOT.parents[1] / ROM_FONT_PATH,
)
# A lossless WebP of the same 16x12 RGB fixture, generated with cwebp -lossless.
WEBP_FIXTURE = "UklGRh4AAABXRUJQVlA4TBEAAAAvD8ACAAdQwIIUvP+BiOh/AAA="


def rom_font() -> Path:
    for directory in ROM_FONTS:
        preferred = directory / "Roboto-Regular.ttf"
        if preferred.is_file():
            return preferred
    candidates = [path for directory in ROM_FONTS for path in sorted(directory.rglob("*"))
                  if path.is_file() and path.suffix.lower() in (".ttf", ".otf")]
    if not candidates:
        raise RuntimeError(f"no ROM font in {', '.join(map(str, ROM_FONTS))}; provide --font")
    return next((path for path in candidates if "roboto" in path.stem.lower()
                 and "regular" in path.stem.lower()), candidates[0])


def png_chunk(tag: bytes, data: bytes) -> bytes:
    payload = tag + data
    return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))


def fixtures(root: Path) -> list[Path]:
    png, jpeg, gif, webp = (root / f"fixture.{name}" for name in ("png", "jpg", "gif", "webp"))
    header = struct.pack(">IIBBBBB", 16, 12, 8, 2, 0, 0, 0)
    row = b"\0" + bytes((32, 128, 224)) * 16
    png.write_bytes(
        b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(row * 12)) + png_chunk(b"IEND", b"")
    )
    for image_format, destination in (("jpeg", jpeg), ("gif", gif)):
        command = ["sips", "-s", "format", image_format]
        if image_format == "jpeg":
            command += ["-s", "formatOptions", "best"]
        subprocess.run([*command, png, "--out", destination], check=True, stdout=subprocess.DEVNULL)
    webp.write_bytes(b64decode(WEBP_FIXTURE))
    return [png, jpeg, gif, webp]


def run_android(ndk: AndroidNdk, sdk: Path, serial: str | None, binary: Path,
                font: Path, images: list[Path]) -> None:
    adb = sdk / "platform-tools/adb"
    if not adb.is_file():
        raise RuntimeError(f"missing Android platform tool: {adb}")
    command = [str(adb)]
    if serial:
        command += ["-s", serial]
    abi = capture([*command, "shell", "getprop", "ro.product.cpu.abi"])
    api = capture([*command, "shell", "getprop", "ro.build.version.sdk"])
    if abi != ANDROID_ABI or not api.isdecimal() or int(api) < ANDROID_API:
        raise RuntimeError(f"expected {ANDROID_ABI} Android API {ANDROID_API}+ device; got {abi} API {api}")
    print(f"Android device: {abi} API {api}", flush=True)

    remote = capture([*command, "shell", "mktemp", "-d", "/data/local/tmp/ogplay-skia.XXXXXX"])
    if not re.fullmatch(r"/data/local/tmp/ogplay-skia\.[A-Za-z0-9]+", remote):
        raise RuntimeError(f"unexpected device staging path: {remote!r}")
    try:
        files = [binary, font, *images]
        run([*command, "push", ndk.runtime, *files, f"{remote}/"], binary.parent)
        run([*command, "shell", "chmod", "755", f"{remote}/{binary.name}"], binary.parent)
        invocation = shlex.join(f"{remote}/{path.name}" for path in files)
        # Android API 23 devices can use ADB's legacy shell protocol.
        result = subprocess.run(
            [*command, "shell", f"LD_LIBRARY_PATH={remote} {invocation}; "
             "status=$?; printf '\nOGPLAY_SKIA_EXIT=%d\n' \"$status\"; exit \"$status\""],
            text=True, capture_output=True, encoding="utf-8",
        )
        print(result.stdout, end="", flush=True)
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        if result.returncode or not re.search(r"^OGPLAY_SKIA_EXIT=0$", result.stdout, re.M):
            raise RuntimeError("Android device smoke test failed")
    finally:
        run([*command, "shell", "rm", "-rf", remote], binary.parent)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--font", type=Path)
    parser.add_argument("--android-sdk", type=Path, default=DEFAULT_ANDROID_SDK)
    parser.add_argument("--serial", help="ADB device serial for the Android smoke test")
    args = parser.parse_args()
    sdk = args.sdk.resolve()
    manifest = verify_sdk(sdk, args.android_sdk)
    cpu = manifest["target_cpu"]
    ndk = AndroidNdk.from_sdk(args.android_sdk) if manifest["platform"] == "android" else None
    label = f"android-{cpu}" if ndk else cpu
    LOCAL.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"smoke-{cpu}-", dir=LOCAL) as scratch:
        root = Path(scratch)
        binary = root / "smoke_skia"
        headers = sdk.parent.parent / manifest["shared_headers"]
        compiler = [str(ndk.clang)] if ndk else ["/usr/bin/clang++", *link_flags(cpu)]
        command = [*compiler, "-std=c++20", "-frtti", "-I", str(headers)]
        command += [f"-D{define}" for define in manifest["consumer_defines"]]
        command += [str(REPOSITORY_ROOT / "tools/smoke_skia.cpp")]
        command += [str(sdk / name) for name in manifest["link_order"]]
        command += [f"-l{name}" for name in manifest["system_libraries"]]
        for framework in manifest["system_frameworks"]:
            command += ["-framework", framework]
        start = time.monotonic()
        run([*command, "-o", binary], root)
        if ndk:
            ndk.verify_executable(binary)
        print(f"{label}: C++20 link passed ({time.monotonic() - start:.2f}s)", flush=True)

        prefix = []
        if not ndk and cpu == "x64" and platform.machine() == "arm64":
            prefix = ["/usr/bin/arch", "-x86_64"]
            probe = subprocess.run([*prefix, "/usr/bin/true"], capture_output=True)
            if probe.returncode:
                print("x64: Rosetta unavailable; runtime checks skipped after successful link")
                return
        if not shutil.which("sips"):
            raise RuntimeError("sips is required to create smoke test fixtures")
        font = args.font.resolve() if args.font else rom_font()
        start = time.monotonic()
        images = fixtures(root)
        if ndk:
            run_android(ndk, args.android_sdk.resolve(), args.serial, binary, font, images)
        else:
            run([*prefix, binary, font, *images], root)
        print(f"{label}: smoke passed ({time.monotonic() - start:.2f}s)")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError) as error:
        sys.exit(f"Skia smoke failed: {error}")
