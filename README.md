# HSR Voice Archive Builder

面向本地角色语音包的 **语音归档、字幕整理、增量更新与训练数据准备工具**。

项目最初为 **Honkai: Star Rail（崩坏：星穹铁道）** 角色语音归档而写；当前 Quick Mode 也已内置对部分可可靠识别的 **Genshin Impact（原神）** 角色语音包与对应索引源的兼容。项目不会附带游戏语音、完整文本数据集或角色资源，所有处理都基于用户自行提供或自行取得的本地素材。

**Current development version:** v0.9-M  
**Web entry:** https://11576865.github.io/HSR-Voice-Archive-Builder/

> Unofficial processing utility. Source packages, extracted audio and finished archives remain on the processing device.

## About

HSR Voice Archive Builder converts local character voice packages into reproducible voice archives: ordered dialogue, verified continuous FLAC, bilingual subtitles, manifests, update metadata, optional ASS/MKV presentation outputs, and GPT-SoVITS-ready training exports. It is designed around local processing, explicit provenance, resumable builds and conservative update handling rather than opaque one-click downloading.

## 项目定位

这个项目解决的不是“播放若干 WAV”，而是把零散角色语音整理成一套 **可复现、可校对、可增量维护** 的长期档案。

核心状态是：

```text
本地角色语音包
        │
        ▼
扫描 / 预检 / 身份与顺序解析
        │
        ├── 本地 LAB / 已有索引
        ├── 官方/社区索引
        ├── 官方中文目标文本
        └── 缺失文本 → 可选 API 翻译
        │
        ▼
Resolved Timeline
        │
        ├── manifest / corrected index
        ├── HSR_Voice_Archive.srt
        ├── continuous.flac
        ├── optional ASS
        └── optional black MKV
```

其中 **Timeline、manifest 和来源信息是机器可复现状态**；字幕样式、ASS、MKV 等属于可重新生成的展示层。

## 当前适用范围

### Honkai: Star Rail

Quick Mode 内置 Star Rail provider 配置，可识别项目使用的常见 archive / chapter / companion / side 文件族，并可使用 AI-Hobbyist 的 EN / CHS / JP / KR 索引进行文本和顺序解析、增量检查以及官方中文对照。

### Genshin Impact

当前代码已内置 Genshin provider、JSON 索引和 Hugging Face 音频数据源支持，并对已经确认的角色文件命名模式进行保守识别，例如：

```text
vo_anecdote_<section>_<character>_<sequence>
```

识别不到的未知文件族不会被强行猜测为 Genshin。对包含完整同名 LAB 的本地语音包，即使远程索引不可用，Quick Mode 仍可使用包内文本和目录结构建立档案。

### Generic voice package

如果语音包本身具有完整、可验证的同名 LAB 文本，项目可以在没有游戏专用远程 provider 的情况下按本地包结构生成档案。此模式不自动获得游戏专用的远程增量更新能力。

内置 provider 当前覆盖的主要源语言为：

```text
English / 简体中文 / 日本語 / 한국어
```

Quick Mode 的最终字幕目标仍以 **简体中文** 为主。

## 核心能力

- **Quick Scan / 预检**：只读扫描 ZIP / 7z / 目录，统计 WAV/LAB、检查角色识别、索引覆盖率、章节分类和阻塞项。
- **可靠排序**：优先使用可验证索引；必要时回退到完整 LAB + 包目录结构，不在证据不足时猜顺序。
- **同名文件消歧**：以 package-relative `source_member_id` 区分并行目录中的同名 WAV，避免不同角色变体或章节互相覆盖。
- **连续无损归档**：构建并校验 `continuous.flac`，保持确定的条目顺序和间隔策略。
- **统一字幕 Timeline**：SRT、ASS 与音频使用同一套 sample-resolved Timeline，避免字幕和音频各自维护顺序。
- **官方中文优先**：安全匹配到官方中文时优先使用；项目也会 use confirmed incremental `Chinese(PRC)` text as official Chinese target text before API fallback.
- **可选翻译补全**：只对仍缺少目标文本的行调用兼容 Responses API 的服务，并保留 provider、模型、checkpoint、token / cost 记录。
- **人工校对覆盖层**：只修改最终字幕，不覆盖官方文本/API来源；之后重建会重新应用人工修订。
- **增量更新**：区分新增条目、既有文本变化、可比较音频哈希变化和冲突记录；自动下载只接纳确认的新记录。
- **恢复与回滚**：构建阶段有 checkpoint；失败重建恢复上一套已发布输出，异常中止会留下 incomplete 标记。
- **ASS 排版工作台**：字体、字号、描边、阴影、模糊、半透明底、淡入淡出、安全区、双语布局、Karaoke 和 Archive HUD。
- **词级时间轴**：可导入 `word_alignments.json`，也可选装 WhisperX；没有可靠对齐时不会伪造逐字 timing。
- **GPT-SoVITS 数据导出**：校验音频 hash、语言声明与来源，生成训练样本与 `sample_provenance.csv`。
- **本地 / LAN 控制**：Windows 与 Termux 都通过本机 Web UI 控制，处理仍发生在宿主设备。

## 快速开始

### Windows

要求：

- Python 3.11+
- FFmpeg
- Git（仅在使用 clone / pull 时需要）

下载仓库后直接运行：

```powershell
git clone https://github.com/11576865/HSR-Voice-Archive-Builder.git
cd HSR-Voice-Archive-Builder
.\run_windows.bat
```

然后打开：

```text
http://127.0.0.1:8765/
```

`run_windows.bat` 会检查 Python，并在需要时补装 `requirements.txt` 中缺失的依赖。

如果 8765 端口被占用：

```powershell
python -m app.launch --port 8766
```

查看依赖和 FFmpeg 状态：

```powershell
python -m app.preflight
```

如果需要让手机或局域网其他设备控制 Windows 主机：

```text
run_windows_lan.bat
```

音频处理和项目文件仍留在 Windows 电脑上。

### Android / Termux

```bash
pkg install -y git
cd ~
git clone https://github.com/11576865/HSR-Voice-Archive-Builder.git
cd HSR-Voice-Archive-Builder
bash run_termux.sh
```

正常更新和启动：

```bash
cd ~/HSR-Voice-Archive-Builder
git pull --ff-only
bash run_termux.sh
```

然后打开：

```text
http://127.0.0.1:8765/
```

Termux 使用轻量 stdlib server；7z 优先走原生 CLI。桌面端缺少 7-Zip CLI 时，Quick Scan 可回退到 `py7zr`。更多说明见 [docs/termux.md](docs/termux.md)。

## Quick Mode

推荐大多数用户从 Quick Mode 开始：

1. 选择本地角色语音包；
2. 执行 **扫描 / 预检**；
3. 查看游戏/角色识别、索引覆盖、章节覆盖、LAB 和翻译缺口；
4. 只有在顺序和来源足够可靠时创建项目；
5. 构建 `continuous.flac`、SRT 与 manifest；
6. 需要时再进行人工字幕校对、ASS 排版、词级时间轴和增量更新。

Quick Scan 的原则是：

> 能验证就采用；无法可靠验证就报告 blocker，而不是猜测。

对于离线环境，可以通过本地索引环境变量覆盖远程 provider。例如 Star Rail：

```bash
export HSR_VOICE_INDEX_FILE=/path/to/EN.xlsx
```

Genshin provider 也支持对应的本地索引覆盖。

## 输出结构

典型固定输出：

```text
output/
├── manifest.json
├── manifest.csv
├── bilingual_index_corrected.csv
├── timeline_resolved.json
├── build_report.json
├── update_plan.json
├── HSR_Voice_Archive.srt
└── continuous.flac
```

按需生成：

```text
HSR_Voice_Archive.ass
HSR_Voice_Archive_Black.mkv
```

ASS and black MKV are on-demand finished outputs.

内部 checkpoint、翻译使用量、QA、恢复状态等保存在项目 `.state/`，不会和面向用户的成品混在一起。

## 字幕与 ASS 工作台

人工校对采用非破坏式 override：

```text
官方/索引/API 文本
        ↓
来源保持不变
        ↓
final subtitle override
        ↓
SRT / ASS
```

ASS 工作台以 1920×1080 为基准画布，默认：

- 横向安全边距 3%
- 保护中央间隙
- 上方源语言 / 下方中文约 60 / 40 区域
- FFmpeg/libass 作为最终渲染依据

Karaoke 只在存在完整、单调递增且通过文本/时长 fingerprint 校验的词级时间轴时输出 `\k` / `\kf` / dynamic clip 效果。没有可靠 timing 时保持普通字幕。

可选 WhisperX：

```bash
python -m pip install whisperx
```

它不是基础依赖，也不会被程序自动安装。

## 增量更新与可靠性

远程检查会分别报告：

- 新文件；
- 已存在条目的官方文本变化；
- 可比较的音频 SHA-256 变化；
- 冲突或身份不明确记录。

自动接纳范围仅限确认的新记录。既有条目发生变化时留给人工检查，不会静默覆盖。

构建流程具有阶段恢复能力，但 **不声称支持 FFmpeg 中途续编码**。完整 FLAC 只有在大小和 SHA-256 仍匹配时才会复用；中途失败则重新编码。

正常重建失败时会恢复之前已发布的 manifest、字幕、FLAC、可选 MKV 与 publication checkpoint。异常进程退出则通过 build marker 把当前输出标记为未完成。

详细规则见 [docs/reliability.md](docs/reliability.md)。

## 翻译配置

翻译完全可选，仅用于仍缺失的目标文本，或显式开启的高风险官方中文复核。

配置兼容 provider：

```bash
python -m app.credentials configure --provider custom --base-url https://example.com/v1
python -m app.credentials test
```

API key 只保存在本机凭据目录，不写入项目 JSON，也不会发送给浏览器前端。

## GPT-SoVITS 导出

项目可以把已完成档案导出为 GPT-SoVITS 训练数据。

导出前会检查：

- source text language；
- declared audio language；
- 源 WAV SHA-256 是否仍与完成档案一致；
- 样本来源信息。

导出结果包含 `sample_provenance.csv`，用于把训练样本重新追溯到原始角色语音资产。

## 文档

- [Architecture](docs/architecture.md)
- [Reliability and recovery](docs/reliability.md)
- [Reference audio workbench](docs/reference_workbench.md)
- [Termux notes](docs/termux.md)
- [ASS layout engine](docs/layout_engine.md)
- [Changelog](CHANGELOG.md)
- [Project notice](NOTICE.md)

GitHub Pages 只作为项目入口和启动说明，不承担实际音频处理。

## 开发与测试

```bash
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -v
python -m pytest -q tests/test_gpt_sovits_export.py
```

GitHub Actions 覆盖 Python 3.11 / 3.12 / 3.13、GPT-SoVITS exporter 测试与轻量 Termux import surface。

测试使用合成 fixture，不需要游戏数据。

## 项目边界

本仓库只提供处理工具，不分发游戏语音、提取资源或完整对白数据集。

Honkai: Star Rail、Genshin Impact 及相关名称、角色、音频、文本与其他游戏资产归其各自权利人所有。本项目为非官方归档/处理工具，与 HoYoverse 无隶属或背书关系。

参见 [NOTICE.md](NOTICE.md)。
