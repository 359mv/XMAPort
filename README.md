# XMAPort

[![GitHub Release](https://img.shields.io/badge/version-260824.Beta-blue)](../../releases)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgrey)](#%E7%B3%BB%E7%BB%9F%E8%A6%81%E6%B1%82)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](#%E8%AE%B8%E5%8F%AF%E8%AF%81)

**XMAPort** 是一款小米 HyperOS 自动化移植（Porting）工具：输入源机型 ROM 与目标底包 ROM 的官方完整包直链，即可自动完成下载、解包、分区迁移、打补丁与重打包，输出可直接刷写的目标机型镜像。

---

## 简介

XMAPort 面向小米 HyperOS 设备的移植玩法：把一台机型的 HyperOS 系统（system / system_ext / product / mi_ext 等分区）迁移到另一台机型的官方底包上，并自动完成特性同步、属性补丁与镜像重打包。

整个流程由主脚本 `XMAPort.py`（Python 3.8+，Windows 平台）驱动，核心迁移逻辑在 `tools/make_hyper.py`，打包逻辑在 `tools/pack_partitions.py`。既支持 Windows 交互式菜单 / 命令行一键模式，也可以直接使用仓库自带的 GitHub Actions 在云端完成构建。

## 特性

- **全自动 7 步流水线**：下载 → 解压卡刷包 → 解包 payload → 解包分区镜像 → 迁移打补丁 → 重打包 → 汇总输出，一条命令跑通
- **多线程下载**：使用 aria2c 多线程下载官方 ROM 完整包直链
- **完整分区迁移**：迁移 system / system_ext / product / mi_ext 到目标底包，自动处理 odm / vendor / vbmeta 等
- **智能特性同步**：特性同步、清理 MIUI booster、同步 APEX、刷新率 / 相机 / 人脸解锁同步、build.prop 补丁
- **联发科支持**：针对天玑 8100 / 8200 的 HWC 补丁
- **灵活打包**：erofs（lz4hc / lz4 / zstd 等压缩算法）或 ext4，可选生成 super.img、sparse 格式、禁用 vbmeta 校验、注入 adb debug
- **云端构建**：自带 GitHub Actions workflow，无需本地环境即可构建并自动发布 Release

## 工作流程

1. **下载 ROM**：使用 aria2c 多线程下载源机型与目标机型的官方 ROM 完整包直链
2. **解压卡刷包**：使用 7-Zip 解压 ROM zip
3. **解包 payload**：使用 payload-dumper-go 解包 `payload.bin` / `.dat`
4. **解包分区镜像**：使用 simg2img / lpunpack / extract.erofs 等工具解包 system、vendor、odm 等分区镜像
5. **迁移打补丁**：核心逻辑在 `tools/make_hyper.py`，把源机型的 system / system_ext / product / mi_ext 迁移到目标底包上，包括特性同步、清理 MIUI booster、sync APEX、刷新率 / 相机 / 人脸解锁同步、build.prop 补丁，以及联发科天玑 8100 / 8200 的 HWC 补丁
6. **重打包**：`tools/pack_partitions.py` 按 erofs（支持 lz4hc / lz4 / zstd 等压缩）或 ext4 打包，可选生成 super.img、生成 sparse 格式、禁用 vbmeta 校验、注入 adb debug
7. **汇总输出**：汇总输出可刷写的目标机型镜像

## 系统要求

- Windows 10 / 11 64 位
- Python 3.8+
- 约 40GB 可用磁盘空间
- 可访问 GitHub 与小米 CDN 的网络环境

## 使用方法

### 方式一：Windows 交互式菜单

直接运行主脚本，按菜单提示操作：

```bat
python XMAPort.py
```

### 方式二：命令行一键模式

```bat
python XMAPort.py --auto --device <目标代号> --source <源ROM直链> --target <底包ROM直链>
```

示例：

```bat
python XMAPort.py --auto --device sky --source https://.../source-rom-full.zip --target https://.../target-rom-full.zip
```

- `--device`：目标设备代号（如 `sky`、`vermeer` 等）
- `--source`：源机型 ROM 完整包直链（不能以 `ultimateota` 开头）
- `--target`：目标底包 ROM 完整包直链（不能以 `ultimateota` 开头）

> 注意：使用前请先按 [配置说明](#配置说明) 检查 `config.ini`，特别是 `device_platform` 与 `device_size`。

### 方式三：GitHub Actions 云端构建

仓库自带 `.github/workflows/build.yml`，无需本地环境：

1. 打开仓库的 **Actions** 页面，选择 **build** workflow
2. 点击 **Run workflow**（`workflow_dispatch` 手动触发），填入：
   - `device`：目标设备代号
   - `source`：源 ROM 完整包直链
   - `target`：底包 ROM 完整包直链
3. 构建完成后会自动分卷压缩 super.img 并发布到 Release

此外，每次 push 也会自动打包源码并发布 `XMAPort-*-Beta` release。

## 配置说明

所有配置集中在 `config.ini`：

### 基本设置（必须核对）

| 配置项 | 说明 |
| --- | --- |
| `device_platform` | 设备平台：`Qualcomm` / `MTK`，**必须如实填写，填错有变砖风险** |
| `device_size` | 目标设备 super 分区总大小（字节），默认 `6979321856`（6.5GB），**必须按设备如实填写** |

### 下载设置

`[source]` / `[target]` 填写源 / 底包 ROM 直链；`[settings]` 控制 aria2c 的下载线程数（`threads`）、最大连接数（`max-connection`）、超时（`timeout`）与重试次数（`retry`）。

### 打包设置（`[packing]`）

| 配置项 | 说明 |
| --- | --- |
| `format` | `erofs` 或 `ext4` |
| `compression` | erofs 压缩算法（如 `lz4hc`、`lz4`、`zstd`） |
| `compression_level` | erofs 压缩等级 |
| `pack_super` | 是否打包生成 super.img |
| `sparse` | 是否生成 sparse 格式镜像 |
| `readonly` | 分区是否设为只读 |
| `metadata_size` / `metadata_slots` | super metadata 大小与插槽数（建议默认 `65536` / `3`） |
| `virtual_ab` | 是否启用 Virtual A/B |
| `enable_adb_debug` | 是否注入 adb debug |
| `patch_vbmeta` | 是否禁用 vbmeta 校验 |
| `is_skip_apex` | 跳过 system_ext 重打包并直接复制源镜像（建议默认） |

其余参数（`utc_stamp`、`super_name`、`super_group`、`erofs_old_kernel`、`ext4_packer` 等）建议保持默认。

### build.prop 补丁列表

文件末尾的 prop 列表会在迁移时写入 build.prop，例如电池快充（`persist.vendor.accelerate.charge`）、夜间充电（`persist.vendor.night.charge`）、默认刷新率（`ro.vendor.display.default_fps`）等。可以自行添加 prop，但**自行添加不保证开机**。

## 注意事项与常见问题

- **ROM 直链**：`--source` / `--target` 必须是官方完整卡刷包（fastboot 线刷包不可用）的直接下载 URL，不能以 `ultimateota` 开头
- **平台填错会变砖**：`device_platform` 是高通还是联发科务必核实清楚
- **super 大小要准确**：`device_size` 与目标设备不符可能导致无法刷入或无法开机
- **磁盘空间**：下载、解包、打包的中间文件较多，建议预留约 40GB
- **杀毒软件误报**：目录中捆绑的第三方 exe（aria2c、7z、mkfs.erofs 等）可能被误报，请添加信任或临时关闭
- **理论支持范围**：小米 11–15、REDMI K50–K90、Note / REDMI 12–15 系列（详见下表实际测试情况）
- **命令行模式找不到设备代号**：目标设备代号即底包 ROM 中 MIUI/HyperOS 版本号后的设备代号（如 `OS2.0.204.0.VMWCNXM` 中的 `sky`）

## 已测试的移植路线

| 源机型 | 目标机型 |
| --- | --- |
| REDMI Note12R | K70 / Note12Turbo / Note17 / 小米12 / 小米17 Ultra |
| Note12T Pro | K90 Max |
| K100 Pro | 小米17 Ultra |

以上为已实测路线；其他同架构机型理论上也可行，但未经验证，请自行测试并承担风险。

## 免责声明

- 本项目**仅供个人学习与测试使用**，请于下载后 24 小时内自行删除相关文件
- 刷机有**变砖**与**数据丢失**风险，使用本项目造成的一切后果由使用者**自行承担**
- 本项目与小米官方**无关**，ROM 版权归小米公司所有
- **禁止商用**

## 许可证

- 项目主代码（`XMAPort.py`、`tools/*.py`）采用 [MIT](LICENSE) 许可证
- 目录中捆绑的第三方工具分别适用其原始许可证：AGPL-3.0 / GPL-2.0 / LGPL-2.1，仓库中有对应的 LICENSE 文件

## 致谢

- 本项目使用 AI 辅助编码（Vibe Coding，DeepSeek / GLM / 小米 MiMo 等）
- 感谢以下开源项目的支持：
  - [aria2](https://github.com/aria2/aria2)、[7-Zip](https://www.7-zip.org/)
  - [payload-dumper-go](https://github.com/xunchangguo/payload-dumper-go)
  - [erofs-utils](https://github.com/erofs/erofs-utils)、[lpunpack / lpmake](https://android.googlesource.com/platform/system/extras/)、e2fsprogs 相关工具
  - 以及所有 HyperOS 移植社区的开发者们

---
---

# XMAPort (English)

**XMAPort** is an automated porting tool for Xiaomi HyperOS: feed it the official full-ROM direct links of a source device and a target base ROM, and it automatically handles downloading, unpacking, partition migration, patching and repacking — producing flashable images for the target device.

---

## Table of Contents

- [Introduction](#introduction-1)
- [Features](#features-1)
- [Workflow](#workflow-1)
- [Requirements](#requirements-1)
- [Usage](#usage-1)
- [Configuration](#configuration-1)
- [Notes & FAQ](#notes--faq-1)
- [Tested Ports](#tested-ports-1)
- [Disclaimer](#disclaimer-1)
- [License](#license-1)
- [Acknowledgements](#acknowledgements-1)

## Introduction

XMAPort is built for the HyperOS porting scene on Xiaomi devices: it migrates one device's HyperOS system partitions (system / system_ext / product / mi_ext, etc.) onto another device's official base ROM, and automatically performs feature syncing, property patching and image repacking.

The whole pipeline is driven by the main script `XMAPort.py` (Python 3.8+, Windows platform). The core migration logic lives in `tools/make_hyper.py` and the packing logic in `tools/pack_partitions.py`. You can run it via an interactive Windows menu, a one-shot CLI mode, or entirely in the cloud using the bundled GitHub Actions workflow.

## Features

- **Fully automated 7-step pipeline**: download → extract recovery ROM → unpack payload → unpack partition images → migrate & patch → repack → collect output, all in one command
- **Multi-threaded downloading**: aria2c multi-threaded download of official full-ROM direct links
- **Full partition migration**: migrates system / system_ext / product / mi_ext onto the target base ROM, with automatic handling of odm / vendor / vbmeta
- **Smart feature syncing**: feature sync, MIUI booster cleanup, APEX sync, refresh-rate / camera / face-unlock sync, build.prop patching
- **MediaTek support**: HWC patches for Dimensity 8100 / 8200
- **Flexible packing**: erofs (with lz4hc / lz4 / zstd compression) or ext4; optional super.img, sparse images, vbmeta verification disabling, and adb debug injection
- **Cloud builds**: bundled GitHub Actions workflow lets you build and publish Releases without a local environment

## Workflow

1. **Download ROMs**: aria2c multi-threaded download of the official full ROMs for the source and target devices
2. **Extract recovery ROM**: unzip the ROM zip with 7-Zip
3. **Unpack payload**: unpack `payload.bin` / `.dat` with payload-dumper-go
4. **Unpack partition images**: unpack system, vendor, odm and other partition images with simg2img / lpunpack / extract.erofs, etc.
5. **Migrate & patch**: the core logic in `tools/make_hyper.py` migrates the source's system / system_ext / product / mi_ext onto the target base ROM — including feature sync, MIUI booster cleanup, APEX sync, refresh-rate / camera / face-unlock sync, build.prop patches, and HWC patches for MediaTek Dimensity 8100 / 8200
6. **Repack**: `tools/pack_partitions.py` packs partitions as erofs (supporting lz4hc / lz4 / zstd and other compression algorithms) or ext4, with optional super.img generation, sparse images, vbmeta verification disabling and adb debug injection
7. **Collect output**: assembles the final flashable images for the target device

## Requirements

- Windows 10 / 11 64-bit
- Python 3.8+
- About 40GB of free disk space
- Network access to GitHub and Xiaomi CDN

## Usage

### Option 1: Interactive menu (Windows)

Run the main script and follow the menu:

```bat
python XMAPort.py
```

### Option 2: One-shot CLI mode

```bat
python XMAPort.py --auto --device <target-codename> --source <source-rom-url> --target <base-rom-url>
```

Example:

```bat
python XMAPort.py --auto --device sky --source https://.../source-rom-full.zip --target https://.../target-rom-full.zip
```

- `--device`: target device codename (e.g. `sky`, `vermeer`)
- `--source`: direct URL of the source device's full ROM (must not start with `ultimateota`)
- `--target`: direct URL of the target base ROM (must not start with `ultimateota`)

> Note: before running, check `config.ini` as described in [Configuration](#configuration-1) — especially `device_platform` and `device_size`.

### Option 3: GitHub Actions cloud build

The repository ships with `.github/workflows/build.yml` — no local environment needed:

1. Open the repo's **Actions** page and select the **build** workflow
2. Click **Run workflow** (`workflow_dispatch`), and fill in:
   - `device`: target device codename
   - `source`: direct URL of the source full ROM
   - `target`: direct URL of the base full ROM
3. When the build finishes, `super.img` is automatically split into volumes and published to a Release

In addition, every push automatically packs the source code and publishes an `XMAPort-*-Beta` release.

## Configuration

All settings live in `config.ini`:

### Essential settings (must verify)

| Key | Description |
| --- | --- |
| `device_platform` | Device platform: `Qualcomm` / `MTK`. **Must be filled in truthfully — a wrong value risks a hard brick.** |
| `device_size` | Total size of the target device's super partition in bytes; default `6979321856` (6.5GB). **Must match your actual device.** |

### Download settings

Put the source / base ROM direct links under `[source]` / `[target]`; `[settings]` controls aria2c's `threads`, `max-connection`, `timeout` and `retry`.

### Packing settings (`[packing]`)

| Key | Description |
| --- | --- |
| `format` | `erofs` or `ext4` |
| `compression` | erofs compression algorithm (e.g. `lz4hc`, `lz4`, `zstd`) |
| `compression_level` | erofs compression level |
| `pack_super` | Whether to pack a super.img |
| `sparse` | Whether to output sparse-format images |
| `readonly` | Whether partitions are marked read-only |
| `metadata_size` / `metadata_slots` | super metadata size and slot count (defaults `65536` / `3` recommended) |
| `virtual_ab` | Whether Virtual A/B is enabled |
| `enable_adb_debug` | Whether to inject adb debug |
| `patch_vbmeta` | Whether to disable vbmeta verification |
| `is_skip_apex` | Skip system_ext repacking and copy the source image directly (keep default) |

Other keys (`utc_stamp`, `super_name`, `super_group`, `erofs_old_kernel`, `ext4_packer`, etc.) are best left at their defaults.

### build.prop patch list

The prop list at the end of the file is written into build.prop during migration — e.g. fast charging (`persist.vendor.accelerate.charge`), night charging (`persist.vendor.night.charge`), default refresh rate (`ro.vendor.display.default_fps`), and so on. You may add your own props, but **custom props are not guaranteed to boot**.

## Notes & FAQ

- **ROM direct links**: `--source` / `--target` must be direct download URLs of official full recovery ROMs (fastboot packages are not supported), and must not start with `ultimateota`
- **Wrong platform can brick your device**: double-check whether the target is Qualcomm or MediaTek before setting `device_platform`
- **super size must be accurate**: a `device_size` that doesn't match the device may make the image unflashable or unbootable
- **Disk space**: downloads, unpacking and repacking produce many intermediate files — reserve about 40GB
- **Antivirus false positives**: the bundled third-party executables (aria2c, 7z, mkfs.erofs, etc.) may be flagged; add trust exclusions or temporarily disable your antivirus
- **Theoretical support**: Xiaomi 11–15, REDMI K50–K90, Note / REDMI 12–15 series (see the table below for what has actually been tested)
- **Finding the device codename**: the codename is the device identifier in the base ROM's version string (e.g. `sky` in `OS2.0.204.0.VMWCNXM`)

## Tested Ports

| Source device | Target device |
| --- | --- |
| REDMI Note12R | K70 / Note12Turbo / Note17 / Xiaomi 12 / Xiaomi 17 Ultra |
| Note12T Pro | K90 Max |
| K100 Pro | Xiaomi 17 Ultra |

These are the verified routes. Other devices with matching architectures may work in theory but are unverified — test at your own risk.

## Disclaimer

- This project is **for personal learning and testing only**; please delete the related files within 24 hours of downloading
- Flashing carries risks of **bricking** and **data loss**. You assume full responsibility for any consequences of using this project
- This project is **not affiliated with Xiaomi**; the ROMs are copyrighted by Xiaomi Inc.
- **Commercial use is prohibited**

## License

- The project's main code (`XMAPort.py`, `tools/*.py`) is licensed under [MIT](LICENSE)
- The bundled third-party tools are governed by their original licenses respectively: AGPL-3.0 / GPL-2.0 / LGPL-2.1, with the corresponding LICENSE files included in the repository

## Acknowledgements

- This project is built with AI-assisted coding (Vibe Coding, using DeepSeek / GLM / Xiaomi MiMo, etc.)
- Thanks to the following open-source projects:
  - [aria2](https://github.com/aria2/aria2), [7-Zip](https://www.7-zip.org/)
  - [payload-dumper-go](https://github.com/xunchangguo/payload-dumper-go)
  - [erofs-utils](https://github.com/erofs/erofs-utils), [lpunpack / lpmake](https://android.googlesource.com/platform/system/extras/), e2fsprogs tools
  - And all developers in the HyperOS porting community
