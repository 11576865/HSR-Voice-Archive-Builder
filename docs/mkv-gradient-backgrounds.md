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
| 图片 | PNG／JPEG／WebP（不超过 16 MiB） | 解码、缩放和补边；图片保存在项目中以便复用 |

另有 720p／1080p 输出分辨率及标准／高画质（H.264 CRF 28／19）选项，帧率保持 1 fps。用户上传图片由原先 4 MiB 提高至 16 MiB，Base64 JSON 请求上限为 24 MiB；2400 万解码像素与单边分辨率约束保持不变；**程序生成的渐变不经过上传接口**。

预览请求调用**真实 FFmpeg** 输出 640×360 PNG，显示独立预览证据。最终 H.264 压缩后色带、显示器色深及播放器色彩管理可能影响观感，不能将 PNG 预览当作最终无损验收。新参数使旧预览失效，重复请求的旧结果不能覆写新参数预览。

## 第一定理：复用与失败研究

先研究成熟项目的成功与公开失效，再确定 HSR 要保留的极小子集：

- [Kdenlive Cairogradient](https://docs.kdenlive.org/en/effects_and_filters/video_effects/generate/cairogradient.html)：成熟线性／径向模式、两个端点、颜色控制；HSR 不需要照搬全部 blend-mode 复杂度。
- [Shotcut gradient 用户报错](https://forum.shotcut.org/t/gradient-filter-flickering-in-preview-and-export/38003)：混合模式曾导致预览与导出闪烁，后已修复；HSR 不引入渐变动画和混合合成层，固定 `seed=1,speed=0.00001`。
- [FFmpeg gradients 源码](https://www.ffmpeg.org/doxygen/8.1/vsrc__gradients_8c_source.html)：现成的线性／径向生成源；HSR 使用 FFmpeg 原生 source，拒绝另写绘图引擎。
- [FFmpeg 滤镜文档](https://ffmpeg.org/ffmpeg-filters.html)：8-bit 渐变可能产生色带，`gradfun` 是播放用算法、不适合在有损编码前宣称可消除色带；本工程不声称无色带。
- [Shotcut release notes](https://shotcut.org/download/releasenotes/)：渐变调色选择器、跨系统缺少控件曾有回归；HSR 的生产 HTML 和真实 Chromium 控件检查均应受 CI 保护。

结论：功能基于成熟处理路径。禁用随机调色与旋转；渐变实际生成一张图片后循环用于长音频，避免对每帧执行高斯模糊；后端严格枚举枚举值、输入颜色与分辨率，防止任意滤镜注入；无原生 FFmpeg `gradients` 的环境明确失败，不静默降级为黑色。

## 成品层级与更新联动

完整 `HSR_Voice_Archive_Bundle.mkv` 不是另一个独立音源，也不是旧版黑屏 MKV 的替换名称。它依赖独立保留的 `continuous.flac`（音频母版）、同一字幕时间轴生成的 `HSR_Voice_Archive.ass` 和 `HSR_Voice_Archive.srt`（字幕旁路输出与新鲜度校验），以及用户选择的背景、可选项目字体。

`bundle_export_status` 为每个项目返回 `state`（missing / unverified / stale / current）、`needs_rebuild`、`can_generate`、`prerequisites` 和 `blockers`。旧 MKV 的文件存在不代表符合当前来源；任一必要来源变更使旧 MKV 标为过期，直到重新生成并逐轨验证后才原子替换。前端完整封装卡片先于独立音频、字幕出现；旧版 `HSR_Voice_Archive_Black.mkv` 仅作为兼容输出显示，不能与最新完整封装混淆。

图片背景的 **16 MiB** 限制跨上传控件、两个服务端、Base64 解码、项目保存和 FFmpeg 预览共用；JSON 请求总量最多 **24 MiB**，FastAPI 根据实际接收字节数限流，Lite 根据 Content-Length 限制。原有 8192 单边、2400 万像素的真实图像安全限制保持不变，字幕特效里的图片预览也使用 16 MiB。并非允许无限大图片或取消解码限制。

## 独立字幕与 MKV 的不同验收条件

当前项目即使只完成独立 SRT 导出，SRT 也可以凭自己的字幕导出回执保持「当前有效」；但生成完整 MKV 必须另外有已验证的 ASS 软字幕、连续 FLAC、非空的 SRT 和处于空闲状态的项目。两者不能共用一个「字幕是否有效」布尔量来标记各个成品。

`bundle_export_status.prerequisites` 因而分别暴露 `srt_current`、`ass_current`、`subtitles_current`、`continuous_flac` 和 `build_idle`，其中 `subtitles_current` 表示**用于完整 MKV 的 ASS/SRT 就绪**，不是「SRT 单独无效」。成品列表独立标记 SRT 和 ASS。只有所有生成前提都满足时 `can_generate` 才为真。已发布 MKV 的 `state=current` 也必须满足这些前提。

`project_summary.output_groups.playback_bundle` 的完整 MKV 项会直接给出来自同一次回执计算的 `freshness`、`needs_rebuild`、`can_generate`、`depends_on` 和 `blockers`，旧版黑屏 MKV 单独标为 `legacy_independent_video`。这避免客户端将「磁盘上存在文件」误解为「成品仍代表最新来源」，同时不破坏旧 `final_products` 接口结构。

### 单个字幕成品的独立验收

字幕导出回执除原有聚合 `state`、`artifacts_current` 外，还分别提供 `srt_current` 与 `ass_current`。它们按同一份输入来源签名和各自的文件哈希判断：只修改 ASS 时，未变化的 SRT 继续是当前有效的独立文本成品；只修改 SRT 时，未变化的 ASS 仍可单独有效。**完整 MKV 的 `subtitles_current` 仍要求两者同时有效**，不因单个文件未变化而放行整个封装。

`project_summary.output_groups.independent_products` 暴露这两个字幕文件的 `current`／`freshness` 与 `used_in_bundle`；连续 FLAC 作为独立无损母版，`ready_for_bundle` 只是非空预检，`freshness=unverified` 不表示已用 FFprobe 独立验证其音频身份。完整 MKV 的 `playback_bundle` 状态继续由来源回执负责，旧黑屏 MKV 不能继承该状态。

## 验收合同

1. `normalize_bundle_background()` 拒绝非法模式、注入型颜色／方向／分辨率／画质。
2. 真实 FFmpeg 输出所有背景模式的有效 PNG；真实 MKV 验证 H.264、FLAC、ASS、字体身份与目标分辨率，解码并比较图像像素。
3. 720p/1080p、CRF 28/19 由同一项目配置与 FFmpeg 导出执行消费，后台封装器保留原有输出的原子替换和失效保护。
4. 两套服务端（FastAPI、Termux Lite）共享背景配置和真正的 FFmpeg 预览路径。浏览器桌面与 390px 手机宽度截图验证控件可见、保存配置回填、预览请求与 PNG 成功解码。
5. 原始图片超过 16 MiB 或 2400 万像素仍拒绝；不能把「渐变不依赖上传文件」误写成取消所有安全限制。
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


## 完整 MKV 与其他成品的依赖及更新状态

- `continuous.flac`：独立无损母版音频，完整 MKV 仅复制其 FLAC 音轨，不会修改源文件。
- `HSR_Voice_Archive.srt` 和 `HSR_Voice_Archive.ass`：基于同一个字幕数据与独立字幕导出回执的成品。完整 MKV 使用通过当前一致性校验的 ASS 软字幕；二者仍可单独保存和使用。
- `HSR_Voice_Archive_Bundle.mkv`：由当前 FLAC、已校验的 ASS/SRT、字体与背景生成的**派生播放成品**。生成前检查源存在、字幕回执状态及项目构建空闲。来源改变或中断后旧 MKV 保留在磁盘，但在成品区标为过期；新文件通过逐轨验证后替换。
- `HSR_Voice_Archive_Black.mkv`：历史兼容的独立黑屏视频输出，没有完整 MKV 的来源状态回执，不应被展示为另一个“当前完整 MKV”。
- 项目 API 的 `output_groups` 保留既有 `final_products`，新增 `playback_bundle`、`independent_products` 与 `legacy_outputs` 供成品区使用；不会自动把完整 MKV 反向拆成 FLAC 或字幕。

图片上传仍通过本机浏览器和服务端的 Base64 JSON 路径，16 MiB 是**原始图像字节上限**，24 MiB 是服务端严格按实际读取字节检查的 JSON 上限，而不是输出 MKV 的体积上限。FFmpeg 解析和解码像素约束保留。
