# OGPlay Skia Prebuilt SDK

此仓库保存 OGPlay 用的可重定位 Skia 静态 SDK。macOS arm64、macOS x64、
Android arm64（`arm64-v8a`）和 Windows x64 共用根目录 `include/` 与 `licenses/`。
各自的 `release/lib/` 提供一个包含第三方实现的静态库：Windows 为 `skia.lib`，
其他平台为 `libskia.a`。最低系统版本为
macOS 13.3 和 Android API 23。

SDK 用于 CPU 软件栅格，画面由调用方交给 ANGLE 上屏。它支持 PNG、JPEG、WebP 和
GIF 解码，以及 PNG、JPEG 编码。GIF 由 Wuffs 解码。字体用 FreeType 栅格化，调用方
从字节提供字体，不查询系统字体。路径布尔运算和简化由各平台静态库中的 PathOps 提供。

使用 `include/ports/SkFontMgr_empty.h` 中的 `SkFontMgr_New_Custom_Empty()` 创建字体
管理器，再调用 `SkFontMgr::makeFromData()` 创建字体。M157 的管理器包含一个无名的
空占位字体族，字体选择及回退由调用方负责。

使用 C++ 编译器链接平台库，将 SDK 的 `include/` 加入头文件搜索路径，保留
`#include "include/core/SkCanvas.h"` 等上游写法。把平台清单中的 `consumer_defines`
传给使用 Skia 头文件的目标，包含 `SK_RELEASE`。macOS 链接 CoreFoundation、
CoreGraphics 和 ImageIO。Android 链接 `log`、`dl` 和宿主统一提供的
`libc++_shared.so`。平台清单还记录 GN 参数、库的链接顺序、大小和 SHA-256。
公共头文件和许可证分别带有自己的 SHA-256 清单。

Windows 库使用 clang-cl 23.1.5、VS 2026 MSVC 工具集 14.51.36231 和
Windows SDK 10.0.28000.0 构建，采用 `/MD`，关闭 Clang trivial ABI。
消费目标使用 C++20、`/MD`、RTTI 和平台清单中的 `consumer_defines`，链接
`Ole32.lib`、`OleAut32.lib`、`FontSub.lib`、`User32.lib`、`Usp10.lib`。
clang-cl 与 MSVC 消费程序均已通过绘图、ROM 字体、编解码、PathOps 和 RTTI 冒烟测试。

SDK 通过 `-frtti`（Windows 为 `/GR`）开启 C++ RTTI，供 OGPlay 宿主继承
`SkWStream` 和 `SkBitmap::Allocator`。

源码固定在 Skia 稳定分支 `chrome/m157` 的
[`682482c8a67b905f76be24cc3c95215e87dbd3f5`](https://skia.googlesource.com/skia/+/682482c8a67b905f76be24cc3c95215e87dbd3f5)。
构建、打包和复验使用 [维护工具](tools/README.md)。

本 SDK 使用 FreeType 2。This software is based in part on the work of the Independent
JPEG Group. 分发时请随附 [许可证](licenses/manifest.json)。
