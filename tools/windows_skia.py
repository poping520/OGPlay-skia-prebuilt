"""Windows x64 clang-cl build, COFF verification and SDK packaging."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time


WINDOWS_LIBRARIES = ["Ole32.lib", "OleAut32.lib", "FontSub.lib", "User32.lib", "Usp10.lib"]


def capture(command, cwd=None):
    return subprocess.check_output(command, cwd=cwd, encoding="utf-8").strip()


class WindowsTools:
    def __init__(self, clang: Path | None = None):
        if sys.platform != "win32":
            raise RuntimeError("Windows SDK builds and verification require Windows")
        root = clang or os.environ.get("SKIA_LLVM")
        if not root:
            compiler = shutil.which("clang-cl")
            root = Path(compiler).parent.parent if compiler else Path("C:/Program Files/LLVM")
        self.root = Path(root).resolve()
        self.bin = self.root / "bin"
        for name in ("clang-cl", "lld-link", "llvm-lib", "llvm-ar", "llvm-nm", "llvm-readobj", "llvm-objcopy"):
            if not (self.bin / f"{name}.exe").is_file():
                raise RuntimeError(f"missing LLVM tool: {self.bin / name}")
        vswhere = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft Visual Studio/Installer/vswhere.exe"
        installation = capture([vswhere, "-latest", "-products", "*", "-requires",
                                "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"])
        if not installation:
            raise RuntimeError("Visual Studio with the x64 C++ tools is required")
        self.vs = Path(installation)
        self.vc = self.vs / "VC"
        self.vc_version = (self.vc / "Auxiliary/Build/Microsoft.VCToolsVersion.default.txt").read_text(encoding="utf-8").strip()
        # Import the matching MSVC standard library, CRT and Windows SDK environment.
        env_dump = capture(f'cmd.exe /d /s /c ""{self.vc / "Auxiliary/Build/vcvars64.bat"}" >nul && set"')
        for line in env_dump.splitlines():
            key, sep, value = line.partition("=")
            if sep and key and not key.startswith("="):
                os.environ[key] = value
        self.sdk = Path(os.environ["WindowsSdkDir"])
        self.sdk_version = os.environ["WindowsSDKVersion"].rstrip("\\/")
        ninja = self.vs / "Common7/IDE/CommonExtensions/Microsoft/CMake/Ninja/ninja.exe"
        self.ninja = str(ninja) if ninja.is_file() else shutil.which("ninja")
        if not self.ninja:
            raise RuntimeError("native Windows Ninja is required")

    def tool(self, name):
        return self.bin / f"{name}.exe"

    def args(self, cpu="x64", portable=False):
        from build_skia import gn_args_for
        if cpu != "x64":
            raise ValueError("Windows SDK currently supports only x64")
        args = dict(item.split("=", 1) for item in gn_args_for(cpu))
        for key in ("cc", "cxx", "extra_asmflags", "extra_cflags", "extra_ldflags"):
            args.pop(key, None)
        args.update(
            target_os='"win"', is_trivial_abi="false", extra_cflags='["/MD"]', extra_cflags_cc='["/GR"]',
            clang_win=json.dumps("<LLVM>" if portable else self.root.as_posix()),
            win_vc=json.dumps("<VC>" if portable else self.vc.as_posix()),
            win_sdk=json.dumps("<SDK>" if portable else self.sdk.as_posix()),
            win_toolchain_version=json.dumps(self.vc_version),
            win_sdk_version=json.dumps(self.sdk_version),
        )
        return [f"{key}={value}" for key, value in sorted(args.items())]


def verify_coff(archive, tools):
    members = capture([tools.tool("llvm-ar"), "t", archive]).splitlines()
    if not members or any(not name.endswith(".obj") for name in members):
        raise RuntimeError("expected nonempty COFF .obj archive")
    if any("SkDocument_PDF_None" in name for name in members):
        raise RuntimeError("disabled PDF placeholder object was not removed")
    dump = capture([tools.tool("llvm-readobj"), "--file-headers", "--sections", "--coff-directives", archive])
    formats = re.findall(r"^Format: (.+)$", dump, re.M)
    if formats != ["COFF-x86-64"] * len(members):
        raise RuntimeError("archive contains unexpected object architecture or format")
    if re.search(r"Name: \.debug\$", dump):
        raise RuntimeError("unexpected debug sections")
    if re.search(r"(?:LIBCMT|MSVCRTD|LIBCMTD)(?:\.lib)?\b", dump, re.I):
        raise RuntimeError("unexpected static or debug CRT dependency")
    if "MSVCRT" not in dump.upper():
        raise RuntimeError("missing /MD CRT directives")
    demangled = capture([tools.tool("llvm-nm"), "--defined-only", "--extern-only", "--demangle", "--format=just-symbols", archive])
    if "SkFontMgr_New_Custom_Empty(" not in demangled or 'SkWStream `RTTI Type Descriptor' not in demangled:
        raise RuntimeError("missing custom font manager or SkWStream RTTI")
    pathops = (" Op(", " Simplify(", " AsWinding(", "SkOpBuilder::add(", "SkOpBuilder::resolve(")
    if missing := [name.strip() for name in pathops if name not in demangled]:
        raise RuntimeError(f"PathOps definitions are missing: {', '.join(missing)}")
    from build_skia import FORBIDDEN_SYMBOL
    # MSVC demangling includes return/argument types. GPU forward declarations
    # in CPU API signatures do not mean a GPU implementation was linked.
    names = "\n".join(re.sub(r"\b(?:class|struct|enum) [\w:]+", "", line.split("(", 1)[0])
                      + ("(" if "(" in line else "") for line in demangled.splitlines())
    if bad := FORBIDDEN_SYMBOL.search(names):
        raise RuntimeError(f"disabled feature found: {bad.group(0)}")
    undefined = capture([tools.tool("llvm-nm"), "--undefined-only", "--format=just-symbols", archive])
    if re.search(r"(?:DWriteCreateFactory|CreateFontIndirect|\b(?:egl|gl)[A-Z])", undefined):
        raise RuntimeError("unexpected system font or GL import")
    print(f"{archive}: {len(members)} COFF x64 objects, /MD, RTTI and PathOps symbol checks passed", flush=True)


def link_check(archive, libraries, tools):
    from build_skia import run
    with tempfile.TemporaryDirectory(dir=archive.parent) as scratch:
        root = Path(scratch)
        source = root / "link.cpp"
        source.write_text("int main() { return 0; }\n", encoding="utf-8")
        run([tools.tool("clang-cl"), "/nologo", "/MD", source, f"/Fe{root / 'link.exe'}",
             "/link", f"/WHOLEARCHIVE:{archive}", *libraries], root)
    print("Windows whole-archive link passed", flush=True)


def fields(tools):
    from build_skia import PINNED_SKIA_COMMIT, MILESTONE, BUNDLED_DEPENDENCIES
    return dict(schema_version=1, skia_commit=PINNED_SKIA_COMMIT, milestone=MILESTONE,
                platform="windows", target_cpu="x64", configuration="release",
                gn_args=tools.args(portable=True), system_frameworks=[],
                cxx_runtime="MSVC /MD", rtti=True, pathops=True, is_trivial_abi=False,
                bundled_dependencies=sorted(BUNDLED_DEPENDENCIES.values()),
                excluded_archive_members=["obj/src/pdf/pdf.SkDocument_PDF_None.obj"])


def sync_windows(source):
    """Fetch only the DEPS needed by the CPU SDK, without modifying upstream."""
    from build_skia import PINNED_SKIA_COMMIT, verify_checkout, run
    if not source.exists():
        source.mkdir(parents=True)
        run(["git", "init", "-b", "main"], source)
    if not (source / "DEPS").exists():
        run(["git", "fetch", "--depth=1", "https://github.com/google/skia.git", PINNED_SKIA_COMMIT], source)
        run(["git", "checkout", "--detach", PINNED_SKIA_COMMIT], source)
    verify_checkout(source)
    scope = {}
    exec("def Var(x): return vars[x]\n" + (source / "DEPS").read_text(encoding="utf-8"), scope)
    names = {"freetype", "libjpeg-turbo", "libpng", "libwebp", "wuffs", "zlib"}
    selected = {path: value for path, value in scope["deps"].items()
                if path.startswith("third_party/externals/") and path.rsplit("/", 1)[-1] in names}
    if len(selected) != len(names):
        raise RuntimeError(f"unexpected Windows dependency inventory: {selected.keys()}")

    def checkout(item):
        path, value = item
        url, commit = value.rsplit("@", 1)
        destination = source / path
        destination.mkdir(parents=True, exist_ok=True)
        if not (destination / ".git").exists():
            run(["git", "init", "-b", "main"], destination)
        current = subprocess.run(["git", "rev-parse", "HEAD"], cwd=destination,
                                 capture_output=True, text=True)
        if current.returncode == 0 and current.stdout.strip() == commit:
            if capture(["git", "status", "--porcelain", "--untracked-files=no"], destination):
                raise RuntimeError(f"modified dependency checkout: {destination}")
            return
        if current.returncode == 0:
            raise RuntimeError(f"dependency revision differs at {destination}; use a clean checkout")
        for attempt in range(3):
            try:
                run(["git", "-c", "http.lowSpeedLimit=1024", "-c", "http.lowSpeedTime=60",
                     "fetch", "--depth=1", url, commit], destination)
                break
            except subprocess.CalledProcessError:
                if attempt == 2:
                    raise
                time.sleep(2)
        run(["git", "checkout", "--detach", commit], destination)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(checkout, selected.items()))
    if not (source / "bin/gn.exe").is_file():
        for attempt in range(3):
            try:
                run([sys.executable, "bin/fetch-gn"], source)
                break
            except subprocess.CalledProcessError:
                if attempt == 2:
                    raise
    verify_checkout(source)


def build_windows(args):
    from build_skia import (LOCAL, BUILD_MANIFEST, verify_checkout,
                            check_config, run, file_record, write_manifest, BUNDLED_DEPENDENCIES, SDK_TARGETS)
    tools = WindowsTools(args.clang)
    config = tools.args(args.cpu or "x64")
    if args.action == "print-config":
        print("\n".join(config))
        return
    source = args.source.resolve()
    output = (args.output or LOCAL / "windows-x64").resolve()
    steps = ("sync", "configure", "build", "verify") if args.action == "all" else (args.action,)
    gn = source / "bin/gn.exe"
    gn_command = [gn, f"--script-executable={sys.executable}"]
    for step in steps:
        if step == "sync":
            sync_windows(source)
            continue
        verify_checkout(source)
        if step == "configure":
            output.mkdir(parents=True, exist_ok=True)
            (output / "args.gn").write_text("\n".join(config) + "\n", encoding="utf-8")
            (output / BUILD_MANIFEST).unlink(missing_ok=True)
            run([gn, "gen", f"--script-executable={sys.executable}", "--fail-on-unused-args", output], source)
            continue
        check_config(output, config)
        archive = output / "sdk/skia.lib"
        if step == "build":
            (output / BUILD_MANIFEST).unlink(missing_ok=True)
            command = [tools.ninja, "-C", output, *SDK_TARGETS]
            if args.jobs:
                command += ["-j", str(args.jobs)]
            run(command, source)
            archive.parent.mkdir(exist_ok=True)
            shutil.copyfile(output / "skia.lib", archive)
            members = capture([tools.tool("llvm-ar"), "t", archive]).splitlines()
            excluded = "obj/src/pdf/pdf.SkDocument_PDF_None.obj"
            if members.count(excluded) != 1:
                raise RuntimeError("expected exactly one disabled PDF placeholder object")
            run([tools.tool("llvm-ar"), "dP", archive, excluded], output)
            desc = json.loads(capture([*gn_command, "desc", output, "//:pathops",
                                       "source_outputs", "--format=json"], source))
            pathops_objects = [output / name for names in desc["//:pathops"]["source_outputs"].values()
                               for name in names if name.endswith(".obj")]
            if not pathops_objects or not all(path.is_file() for path in pathops_objects):
                raise RuntimeError("GN returned missing or empty PathOps object files")
            run([tools.tool("llvm-ar"), "r", archive, *pathops_objects], output)
            run([tools.tool("llvm-objcopy"), "--strip-debug", archive], output)
            continue
        bundled = {label.split(":", 1)[0] for target in SDK_TARGETS
                   for label in capture([*gn_command, "desc", output, f"//:{target}", "deps", "--all"], source).splitlines()
                   if label.startswith(("//modules/", "//third_party/"))}
        if bundled != BUNDLED_DEPENDENCIES.keys():
            raise RuntimeError(f"unexpected bundled dependencies: {sorted(bundled)}")
        desc = json.loads(capture([*gn_command, "desc", output, "//:public_headers_warnings_check", "defines", "--format=json"], source))
        defines = next(iter(desc.values()))["defines"]
        defines = sorted({"SK_RELEASE"} | {x for x in defines if x.startswith("SK_") and x != "SK_USE_INTERNAL_VULKAN_HEADERS"})
        desc = json.loads(capture([*gn_command, "desc", output, "//:skia", "libs", "--format=json"], source))
        libraries = next(iter(desc.values())).get("libs", [])
        libraries = [x if x.lower().endswith(".lib") else x + ".lib" for x in libraries]
        if libraries != WINDOWS_LIBRARIES:
            raise RuntimeError(f"unexpected Windows system library dependencies: {libraries}")
        verify_coff(archive, tools)
        link_check(archive, libraries, tools)
        manifest = {**fields(tools), "system_libraries": libraries, "consumer_defines": defines,
                    "compiler": capture([tools.tool("clang-cl"), "--version"]).replace(str(tools.root), "<LLVM>"),
                    "msvc_toolset": tools.vc_version, "windows_sdk": tools.sdk_version,
                    "targets": [f"//:{target}" for target in SDK_TARGETS], "artifacts": ["sdk/skia.lib"],
                    "files": [file_record(archive, output)]}
        write_manifest(output / BUILD_MANIFEST, manifest)


def verify_windows_sdk(sdk, clang=None):
    from package_skia_sdk import read_json, verify_files, verify_headers, verify_licenses, REQUIRED_DEFINES
    manifest = read_json(sdk / "manifest.json")
    tools = WindowsTools(clang)
    expected = fields(tools)
    # Verification can use a newer local SDK/toolset than the recorded build.
    versions = {"win_toolchain_version": manifest.get("msvc_toolset"),
                "win_sdk_version": manifest.get("windows_sdk")}
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", value)
           for value in versions.values()):
        raise RuntimeError("invalid Windows toolchain version metadata")
    expected["gn_args"] = sorted(
        [arg for arg in expected["gn_args"] if arg.split("=", 1)[0] not in versions]
        + [f"{key}={json.dumps(value)}" for key, value in versions.items()])
    if (any(manifest.get(key) != value for key, value in expected.items())
            or manifest.get("system_libraries") != WINDOWS_LIBRARIES
            or manifest.get("link_order") != ["lib/skia.lib"]
            or manifest.get("shared_headers") != "include" or manifest.get("shared_licenses") != "licenses"
            or not isinstance(manifest.get("consumer_defines"), list)):
        raise RuntimeError("invalid Windows Skia SDK manifest")
    defines = manifest["consumer_defines"]
    if (not all(isinstance(define, str) and re.fullmatch(r"SK_\w+(?:=\w+)?", define) for define in defines)
            or defines != sorted(set(defines)) or not REQUIRED_DEFINES <= set(defines)
            or set(defines) & {"SK_DEBUG", "SK_GANESH", "SK_GRAPHITE", "SK_GL", "SK_SUPPORT_PDF"}):
        raise RuntimeError("invalid Windows Skia consumer defines")
    if verify_files(sdk, manifest.get("files")) != {"lib/skia.lib"}:
        raise RuntimeError("unexpected Windows SDK files")
    verify_headers(sdk.parent.parent / "include")
    licensed = verify_licenses(sdk.parent.parent / "licenses")
    if not set(manifest["bundled_dependencies"]) <= licensed:
        raise RuntimeError("missing Windows SDK dependency licenses")
    verify_coff(sdk / "lib/skia.lib", tools)
    return manifest


def package_windows_sdk(build, root):
    from build_skia import BUILD_MANIFEST, file_record, write_manifest
    from package_skia_sdk import read_json, staging, copy_file, manifest_files
    manifest = read_json(build / BUILD_MANIFEST)
    archive = build / "sdk/skia.lib"
    if manifest.get("artifacts") != ["sdk/skia.lib"] or manifest.get("files") != [file_record(archive, build)]:
        raise RuntimeError("Windows build differs from verified manifest")
    destination = root / "windows-x64/release"
    with staging(destination) as staged:
        copy_file(archive, staged / "lib/skia.lib")
        packaged = {key: value for key, value in manifest.items() if key not in ("targets", "artifacts", "files")}
        packaged.update(link_order=["lib/skia.lib"], shared_headers="include", shared_licenses="licenses",
                        files=manifest_files(staged))
        write_manifest(staged / "manifest.json", packaged)
        verify_windows_sdk(staged)
    return destination
