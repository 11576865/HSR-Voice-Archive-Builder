# HSR MKV 背景：现有方案研究与工程验收

## 目的与边界

HSR 是语音与字幕档案构建器，不承担通用 Matroska Remux。完整 MKV 仍由最新 FLAC（直接复制无损轨）、ASS 软字幕、项目字体附件和一条静态 H.264 视频轨组成。验证新文件后才替换旧文件；旧文件过期状态由 `archive_bundle_receipt.json` 跟踪。

支持以下背景模式，统一从「导出 → 完整 MKV」进入：

| 模式 | 输入 | FFmpeg 路径 |
| --- | --- | --- |
| 纯色 | RGB 色值 | `color` lavfi |
| 线性渐变 | 起止颜色、对角线／水平／垂直 | `gradients` lavfi |
| 径向渐变 | 中心色、外围色 | `gradients:type=radial` |
| 柔和渐变 | 三个色点 | `gradients:type=radial` 加 `gblur` |
| 图片 | PNG／JPEG／WebP（不超过 4 MiB） | 解码、缩放和补边；图片保存在项目中以便复用 |

另有 720p／1080p 输出分辨率及标准／高画质（H.264 CRF 28／19）选项，帧率保持 1 fps。用户上传图片的既有文件大小和 2400 万解码像素约束保持不变；**程序生成的渐变不经过上传接口**。

预览请求调用**真实 FFmpeg** 输出 640×360 PNG，显示独立预览证据。最终 H.264 压缩后色带、显示器色深及播放器色彩管理可能影响观感，不能将 PNG 预览当作最终无损验收。新参数使旧预览失效，重复请求的旧结果不能覆写新参数预览。

## 第一定理：复用与失败研究

先研究成熟项目的成功与公开失效，再确定 HSR 要保留的极小子集：

- [Kdenlive Cairogradient](https://docs.kdenlive.org/en/effects_and_filters/video_effects/generate/cairogradient.html)：成熟线性／径向模式、两个端点、颜色控制；HSR 不需要照搬全部 blend-mode 复杂度。
- [Shotcut gradient 用户报错](https://forum.shotcut.org/t/gradient-filter-flickering-in-preview-and-export/38003)：混合模式曾导致预览与导出闪烁，后已修复；HSR 不引入渐变动画和混合合成层，固定 `seed=1,speed=0.00001`。
- [FFmpeg gradients 源码](https://www.ffmpeg.org/doxygen/8.1/vsrc__gradients_8c_source.html)：现成的线性／径向生成源；HSR 使用 FFmpeg 原生 source，拒绝另写绘图引擎。
- [FFmpeg 滤镜文档](https://ffmpeg.org/ffmpeg-filters.html)：8-bit 渐变可能产生色带，`gradfun` 是播放用算法、不适合在有损编码前宣称可消除色带；本工程不声称无色带。
- [Shotcut release notes](https://shotcut.org/download/releasenotes/)：渐变调色选择器、跨系统缺少控件曾有回归；HSR 的生产 HTML 和真实 Chromium 控件检查均应受 CI 保护。

结论：功能基于成熟处理路径。禁用随机调色与旋转；渐变实际生成一张图片后循环用于长音频，避免对每帧执行高斯模糊；后端严格枚举枚举值、输入颜色与分辨率，防止任意滤镜注入；无原生 FFmpeg `gradients` 的环境明确失败，不静默降级为黑色。

## 验收合同

1. `normalize_bundle_background()` 拒绝非法模式、注入型颜色／方向／分辨率／画质。
2. 真实 FFmpeg 输出所有背景模式的有效 PNG；真实 MKV 验证 H.264、FLAC、ASS、字体身份与目标分辨率，解码并比较图像像素。
3. 720p/1080p、CRF 28/19 由同一项目配置与 FFmpeg 导出执行消费，后台封装器保留原有输出的原子替换和失效保护。
4. 两套服务端（FastAPI、Termux Lite）共享背景配置和真正的 FFmpeg 预览路径。浏览器桌面与 390px 手机宽度截图验证控件可见、保存配置回填、预览请求与 PNG 成功解码。
5. 原始图片超过 4 MiB 或 2400 万像素仍拒绝；不能把「渐变不依赖上传文件」误写成取消所有安全限制。
6. 真实 Android 物理设备、终端播放器色彩管理、持续运行大批量多小时档案是**独立验收范围**，当前 CI 不代表已完成。

默认仍是纯色 + 720p + 标准画质，旧保存的纯色／图片配置兼容。新背景方案不扩大视频来源、上传 MIME 或允许任意 FFmpeg filtergraph 输入。


## 成品 MKV 的只读技术核查（Issue #158 的证据准备）

Windows、Linux 和 Android/Termux 均可使用 **Python 标准库 + FFmpeg/FFprobe** 运行已提交的技术核查工具。先按照正常 HSR 流程生成完整 MKV，再在运行机器上执行：

```bash
python scripts/hsr_mkv_acceptance_probe.py \
  /path/to/project/output/HSR_Voice_Archive_Bundle.mkv \
  --evidence-dir /path/to/acceptance-evidence
```

Windows PowerShell：

```powershell
python scripts/hsr_mkv_acceptance_probe.py "D:\HSR\project\output\HSR_Voice_Archive_Bundle.mkv" --evidence-dir "D:\HSR\acceptance-evidence"
```

预期产物是 `report.json` 和 `decoded_h264_0.png`、`decoded_h264_1.png`（极短视频仅有第一张）。该工具只读取 MKV；解码截图来自**已经封装、经过 H.264 压缩的成品**，而不是用户预览 PNG 或仅由渐变滤镜生成的源图片。

报告记录：目标文件尺寸/修改时间、FFmpeg/FFprobe 版本、可用的 `gradients`/`gblur`/`ass` 滤镜、容器格式、H.264+FLAC+ASS 轨道、字体附件名、可验证时长、解码后 PNG 的 SHA-256 与实际解码耗时。Android/Termux 可补充读取系统版本与设备型号，但不会读取设备序列号、账号信息或用户目录。

**严格的证据边界：** `status=technical_pass` 只证明本机 FFprobe 可识别结构，且 FFmpeg 能成功解码两张最终 H.264 画面。不证明 Android 播放器能正确选中 ASS、渲染附件字体，也不证明听到 FLAC、渐变无色带或长时档案编码效率合格。报告中的 `device_playback`、`visual_banding`、`long_duration_performance` 始终保留 `unverified`，需要实际播放器和硬件证据才能更新 [Issue #158](https://github.com/11576865/HSR-Voice-Archive-Builder/issues/158)。

测试证据使用 `tests/test_hsr_mkv_acceptance_probe.py`：包括错误容器/轨道/字体身份拒绝、缺失工具时 fail-closed、无法解码时不误报通过、报告与原始 MKV 分离，以及合成真实 H.264+FLAC+ASS MKV 的双帧解码。CI 环境成功不等于在实际 Android/Termux 设备上运行成功。
