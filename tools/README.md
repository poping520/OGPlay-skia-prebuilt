# Skia SDK 维护工具

## 构建

需要 macOS Command Line Tools、Python 3.9+、Git 和 Ninja。构建使用
`/usr/bin/clang`、`/usr/bin/clang++` 和 `xcrun` 选中的 macOS SDK。
`sync` 在缺少源码时从 Skia 官方 Git 获取固定提交。已有 checkout 必须在同一提交，
且上游跟踪文件须保持干净。Skia 自带的 `tools/git-sync-deps` 同步 DEPS 并调用
`bin/fetch-gn`，通过 `GIT_SYNC_DEPS_SKIP_EMSDK=1` 跳过 emsdk 激活。

```sh
python3 tools/build_skia.py all --platform macos --cpu arm64
python3 tools/build_skia.py all --platform macos --cpu x64
python3 tools/build_skia.py print-config --cpu arm64
python3 tools/build_skia.py print-config --cpu x64
```

`sync`、`configure`、`build`、`verify` 可分别执行。用 `--jobs` 限制 Ninja 并发数，
用 `--source` 或 `--output` 指定本地目录。默认源码在 `skia/`，构建在
`.local/macos-<cpu>/`。两个架构分别交给 GN 选择工具链，Apple Silicon 可直接交叉
编译 x64。请依次运行两个 `all`，使它们共用同一份源码和同步状态。

### M157 的 GN 参数和静态库

- 官方 Release 构建用 `is_official_build=true`、`is_debug=false`、
  `is_component_build=false`。M157 独立 GN 构建没有 `symbol_level`，官方构建
  自带无调试符号的配置，并显式传入 `-g0`。其默认 macOS 目标三元组含 11.0，
  编译、汇编和链接的 extra flags 用最后生效的 `--target=…-apple-macos13.3`
  固定最低版本。
- macOS 和 Android 都用 `extra_cflags_cc=["-frtti"]` 开启 C++ RTTI，以便
  OGPlay 宿主继承 `SkWStream` 和 `SkBitmap::Allocator`。Ninja 的 C++ 编译
  命令中 `-frtti` 在上游 `-fno-rtti` 之后，C++ 异常保持关闭。
- FreeType 的实际开关为 `skia_use_freetype` 和 `skia_use_system_freetype2`。
  `skia_enable_fontmgr_custom_empty=true`，`skia_use_fonthost_mac=false`，其余
  字体管理器及 FreeType SVG、WOFF2 支持均关闭。FreeType 与 PNG 共用仓库自带的
  Chromium zlib。
- `skia_use_libpng_{decode,encode}` 和 `skia_use_libjpeg_turbo_{decode,encode}`
  控制 PNG/JPEG，`skia_use_libwebp_decode` 控制 WebP，`skia_use_wuffs` 控制 GIF。
  当前目标图声明的五个 `skia_use_system_*` 参数均为 `false`。构建用
  `--fail-on-unused-args` 检查参数，避免因上游改名而静默退回默认配置。
- Ganesh、Graphite、GL、Metal、Vulkan、PDF、SVG、Skottie、SkShaper、
  SkParagraph、ICU、HarfBuzz、expat、DNG/piex 和开发工具均关闭。构建入口
  请求 `//:skia` 和 `//:pathops`，因此也不构建 Skia 测试。
- M157 的 CPU 配置关闭 PDF 后，静态 `//:skia` 不包含独立的 PathOps 目标。构建脚本
  从 GN 查询 `//:pathops` 的对象文件并合入 SDK 的 `libskia.a`。脚本也从 SDK 副本
  删除两个无引用的 CoreText 辅助对象和关闭 PDF 后的占位对象，清单记录删除项。
- macOS 端仍含 Skia 的 CG 图像桥接。整库强制链接验证所需框架为 CoreFoundation、
  CoreGraphics 和 ImageIO，C++ 驱动提供 libc++ 和 libSystem。消费宏取自 GN 的
  上游头文件检查目标，排除该目标自用的 Vulkan 头宏，并用 `SK_RELEASE` 表达
  Skia 的 Release 头文件模式。

`verify` 检查 GN 依赖图、架构、每个对象的最低系统版本、调试段、禁用功能符号
以及 PathOps 和 `SkWStream` RTTI 的符号定义，
执行整库链接并写入 `ogplay-skia-manifest.json`。最终库在输出目录的
`sdk/libskia.a`。

## Android arm64

使用 macOS 上的 Android NDK r30（`30.0.16248370`）构建 `arm64-v8a`，
最低 API 23。默认 Android SDK 目录为 `~/Library/Android/sdk`，可通过
`--android-sdk` 指定实际位置。GN 输出位于 `.local/android-arm64/`。

```sh
sdk=~/Library/Android/sdk
python3 tools/build_skia.py all --platform android --cpu arm64 --android-sdk "$sdk"
python3 tools/build_skia.py print-config --platform android --android-sdk "$sdk"
python3 tools/package_skia_sdk.py package-headers --headers .local/skia-sdk/include
python3 tools/package_skia_sdk.py package-licenses --licenses .local/skia-sdk/licenses --android-sdk "$sdk"
python3 tools/package_skia_sdk.py package --build .local/android-arm64 --android-sdk "$sdk"
python3 tools/package_skia_sdk.py verify --sdk .local/skia-sdk/android-arm64/release --android-sdk "$sdk"
python3 tools/smoke_skia_sdk.py --sdk .local/skia-sdk/android-arm64/release --android-sdk "$sdk"
```

Android 沿用 CPU 栅格与自带编解码器，显式关闭 Android 专用字体 gamma，使 FreeType
字体配置与 macOS 一致。M157 的 Android GN 目标无条件拉入 expat 和 NDK
cpu-features。两者随静态库分发，许可证归入共用目录。清单用 `<NDK>` 表示本机
NDK 路径。构建验证检查归档中每个 ELF 对象，并用 API 23 sysroot 做整库链接，读取
可执行文件的 Android API 标记和动态依赖。整库链接只显式需要 `log`、`dl`，
C++ 驱动链接宿主应用统一提供的 NDK `libc++_shared.so`。

冒烟测试通过 ADB 在连接的 arm64 设备上运行。多设备时可用 `--serial` 选择。
测试临时推送可执行文件、NDK libc++、ROM 字体和测试图，运行完清理设备目录。
API 25 真机已通过，旧 linker 对可执行文件的 `DT_FLAGS_1` 和 NDK libc++ 的
一个动态条目打印警告，程序正常退出。API 23 已验证编译和链接，运行待实测。

## 打包

公共头文件保留 Skia 的路径，只包含 SDK 公共接口和其传递包含的头文件。许可证目录
包含 Skia、skcms、FreeType、libpng、libjpeg-turbo、libwebp、Wuffs、zlib，以及编入
源码的附加许可。Chromium zlib 的 BSD 条款来自
[Chromium 157.0.8103.38 的 LICENSE](https://chromium.googlesource.com/chromium/src/+/refs/tags/157.0.8103.38/LICENSE)。

```sh
python3 tools/package_skia_sdk.py package-headers --headers .local/skia-sdk/include
python3 tools/package_skia_sdk.py package-licenses --licenses .local/skia-sdk/licenses
python3 tools/package_skia_sdk.py package --build .local/macos-arm64
python3 tools/package_skia_sdk.py package --build .local/macos-x64
```

平台包默认生成在 `.local/skia-sdk/<platform>-<cpu>/release`。暂存目录应与共用的头文件
和许可证在同一 SDK 根下。生成目标必须不存在。首次发布时将已验证的
`include/`、`licenses/` 和对应平台目录移到仓库根目录。暂存 Android 包时为
`package-licenses` 传入 `--android-sdk`，生成的共用许可证也可用于 macOS。
源码、GN 输出和暂存包由 `.gitignore` 排除。

## 自测

```sh
python3 tools/build_skia.py --self-test
python3 tools/package_skia_sdk.py --self-test
python3 tools/package_skia_sdk.py verify-headers
python3 tools/package_skia_sdk.py verify-licenses
python3 tools/package_skia_sdk.py verify --sdk macos-arm64/release
python3 tools/package_skia_sdk.py verify --sdk macos-x64/release
python3 tools/package_skia_sdk.py verify --sdk android-arm64/release
python3 tools/smoke_skia_sdk.py --sdk macos-arm64/release
python3 tools/smoke_skia_sdk.py --sdk macos-x64/release
python3 tools/smoke_skia_sdk.py --sdk android-arm64/release
```

冒烟程序只用 SDK 的头文件、库、消费宏和链接参数，以 C++20 和 `-frtti` 编译。
它验证栅格像素、抗锯齿、24px ROM 字体、PNG/JPEG 往返和 GIF/WebP 解码。
PathOps 检查实际调用 `Op`、`Simplify` 和 `SkOpBuilder`，验证结果路径的包含关系。
RTTI 子类检查在自定义 `SkBitmap::Allocator` 提供的像素内存上绘制，再将 PNG 写入
自定义 `SkWStream`，核对像素和写入字节数。字体默认读取相邻 OGPlay 仓库或子模块
所属 OGPlay 仓库的 Android 19 ROM，可用 `--font` 指定本地字体。测试图由脚本生成或
内嵌，临时文件位于 `.local/`。Apple Silicon 上通过 Rosetta 执行 x64。
若 Rosetta 不可用，脚本仍执行链接并报告跳过运行。

## 后续平台

- Windows 按 CPU 单独生成 COFF 静态库，和宿主对齐 MSVC ABI、CRT 及 Release 宏。
  核查 GN Windows 端的系统库依赖，并保持 DirectWrite/GDI 字体管理器关闭。
- iOS 分别使用 `iphoneos` 和 `iphonesimulator` SDK。设备与模拟器各自核对
  Mach-O 平台标记、最低版本及链接框架，继续检查 CoreText 符号和 ROM 字体输出。
