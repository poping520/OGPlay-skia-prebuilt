"""NDK and ELF checks for the Android arm64 Skia package."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess


DEFAULT_ANDROID_SDK = Path.home() / "Library/Android/sdk"
NDK_REVISION = "30.0.16248370"
ANDROID_API = 23
ANDROID_ABI = "arm64-v8a"
ANDROID_LIBRARIES = ("log", "dl")
ANDROID_EXCLUDED_MEMBERS = ("pdf.SkDocument_PDF_None.o",)
ANDROID_DEPENDENCIES = {
    "//third_party/cpu-features": "ndk-cpu-features",
    "//third_party/expat": "expat",
}


@dataclass(frozen=True)
class AndroidNdk:
    root: Path

    @classmethod
    def from_sdk(cls, sdk: Path) -> AndroidNdk:
        ndk = cls(sdk.resolve() / "ndk" / NDK_REVISION)
        properties = ndk.root / "source.properties"
        expected = f"Pkg.Revision = {NDK_REVISION}"
        if not properties.is_file() or expected not in properties.read_text(encoding="utf-8").splitlines():
            raise RuntimeError(f"Android NDK {NDK_REVISION} is required at {ndk.root}")
        for executable in (
            ndk.clang, ndk.bin / "llvm-ar", ndk.bin / "llvm-nm", ndk.bin / "llvm-readelf",
        ):
            if not executable.is_file():
                raise RuntimeError(f"missing NDK tool: {executable}")
        return ndk

    @property
    def bin(self) -> Path:
        return self.root / "toolchains/llvm/prebuilt/darwin-x86_64/bin"

    @property
    def clang(self) -> Path:
        return self.bin / f"aarch64-linux-android{ANDROID_API}-clang++"

    @property
    def runtime(self) -> Path:
        return self.bin.parent / "sysroot/usr/lib/aarch64-linux-android/libc++_shared.so"

    @property
    def cpu_features(self) -> Path:
        return self.root / "sources/android/cpufeatures"

    def readelf(self, path: Path, *arguments: str) -> str:
        return subprocess.check_output(
            [self.bin / "llvm-readelf", *arguments, path], encoding="utf-8"
        )

    def verify_objects(self, archive: Path, member_count: int) -> None:
        dump = self.readelf(archive, "--file-header", "--sections")
        for label, prefix in (
            ("Class", "ELF64"), ("Data", "2's complement, little endian"),
            ("Type", "REL "), ("Machine", "AArch64"),
        ):
            values = re.findall(rf"^\s*{label}:\s*(.+)$", dump, re.M)
            if len(values) != member_count or any(not value.startswith(prefix) for value in values):
                raise RuntimeError(f"{archive}: expected {member_count} ELF AArch64 objects ({label})")
        if re.search(r"\s\.(?:z?debug)[_\w]*\s", dump):
            raise RuntimeError(f"unexpected debug sections in {archive}")

    def verify_executable(self, path: Path) -> None:
        dump = self.readelf(path, "--notes", "--dynamic")
        note = re.search(r"NT_ANDROID_TYPE_IDENT\s*\n\s*description data: ((?:[\da-fA-F]{2} ){3}[\da-fA-F]{2})",
                         dump)
        if not note or int.from_bytes(bytes.fromhex(note[1]), "little") != ANDROID_API:
            raise RuntimeError(f"missing Android API {ANDROID_API} ELF note in {path}")
        needed = set(re.findall(r"Shared library: \[([^\]]+)\]", dump))
        allowed = {"libc++_shared.so", "libc.so", "libm.so", *(f"lib{name}.so" for name in ANDROID_LIBRARIES)}
        if "libc++_shared.so" not in needed or not needed <= allowed:
            raise RuntimeError(f"unexpected Android runtime dependencies in {path}: {sorted(needed)}")
