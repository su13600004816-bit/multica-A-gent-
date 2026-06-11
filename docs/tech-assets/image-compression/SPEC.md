# 图片压缩（image-compression）· 独立技术储备说明书

> **独立体**：本技术是自包含模块，可整块复制到任何新系统。本目录 `package/` 即完整可搬运代码。
> 维护人：总管。最后更新：2026-06-11。

## 0. 一句话
上传图片在**客户端浏览器内**先压成长边 ≤1568px 的 WebP（质量 0.8/0.7/0.6 递降），为发给主脑（Anthropic Vision）省 token + 减小上传体积；非图片/压缩失败自动回退原文件，绝不阻断上传。

## 1. 作用与原理
- 用浏览器原生 `createImageBitmap` 解码 → canvas `drawImage` 缩放到长边 ≤1568px → `canvas.toBlob('image/webp', q)` 编码。
- 1568px = Anthropic Vision 最优长边，直接卡住 token 上限；WebP 比 JPEG 同质更小。
- 质量递降（0.8→0.7→0.6）直到压到 ≤2MB；仍超则回退原文件（不阻断）。

## 2. 本包文件清单（package/，即可搬运的独立体）
| 文件 | 作用 |
|---|---|
| `package/image-compress.ts` | 核心：`compressImage()` + `compressImages()` + `validateCompressedImage()` + `isImageFile()` + `formatFileSize()` |
| `package/config.ts` | 常量：`MAX_IMAGE_LONG_EDGE=1568` / `MAX_UPLOAD_IMAGE_BYTES=2MB` / `IMAGE_COMPRESSION_QUALITY=0.8` / `CLAUDE_IMAGE_TYPE='image/webp'` |
| `package/index.ts` | 统一导出入口 |
| `package/WIRING.example.ts` | 接入示例（如何接到上传入口） |

## 3. 依赖
- **零第三方依赖**：纯浏览器 Web API（`createImageBitmap` / `<canvas>` / `toBlob` / `File`）。
- **运行环境**：仅客户端（`"use client"`）；SSR/Node 不执行（函数内才碰 DOM，import 安全）。
- **TS**：strict 可过（已验，需 `lib: dom`）。

## 4. 对外接口
- `compressImage(file: File): Promise<CompressionResult>` —— 主入口。返回 `{ file, originalSize, compressedSize, reason, usedOriginal, ... }`；**调用方只需用 `result.file`**。
- `compressImages(files): Promise<CompressionResult[]>`、`isImageFile(file)`、`formatFileSize(bytes)`、`validateCompressedImage(file)`。

## 5. 迁移 / 接入新系统（一键迁移步骤）
1. 把整个 `package/` 复制进新系统（建议 `<core>/image-compress/`）。
2. 在新系统的**文件上传入口**，传图前调 `compressImage`，用 `result.file` 替原 `file`（见 `WIRING.example.ts`）。multica 中即 `packages/core/hooks/use-file-upload.ts`。
3. 如需改尺寸/质量上限，改 `config.ts` 常量即可，无需动核心。
4. 验证：见第 9 节。

## 6. 与新系统/上游的同步
- **来源**：移植自老站 ouroboros `/home/fleet/canvas/src/lib/imageCompress.ts`（生产验证过的实现）。
- **同步方向**：本包是 SSOT。若老站/multica 内的副本有改进 → 同步回本包；新系统从本包取。
- **同步检查**：`diff package/image-compress.ts <目标系统副本>`；逻辑分叉以本包为准（除非目标有合理增强，则反向并回本包并在此登记）。

## 7. 改进指南（可扩展点）
- **移动端（P2，未做）**：移动端浏览器外（如 React Native/Expo）无 canvas，需用 `expo-image-manipulator` 的 `manipulateAsync` 等价实现 resize+WebP；接口对齐 `compressImage`。
- **服务端兜底（可选）**：若要服务端再校验/缩略图，老站有 `server/webp.ts`（纯 Buffer WebP 头解析）可参考；当前本包只做客户端。
- **参数**：长边/质量/字节上限均在 `config.ts`；HEIC 解码依赖浏览器，已有友好报错。

## 8. 删除 / 卸载步骤
1. 上传入口移除 `compressImage` 调用，恢复直接传 `file`。
2. 删除目标系统里的 `image-compress/` 目录。
3. 无遗留：本包零依赖、零数据库、零环境变量、零后端改动，删除不影响其它功能。

## 9. 验证方法（真跑，不信"做好了"）
- `tsc --noEmit`（strict + dom lib）过。
- 真浏览器跑：喂一张大图给 `compressImage`，断言 `outType==='image/webp' && longEdge<=1568 && outSize<origSize`。
- **已实测（2026-06-11，真 chromium）**：3000×2000 PNG 122KB → 1568×1045 WebP 9KB（省 93%）PASS。

## 10. 已知限制
- 仅 Web；移动端原生入口未覆盖（P2）。
- 依赖浏览器 WebP 编码（现代浏览器全支持）；HEIC 需浏览器能解码，否则回退原文件并提示。

## 11. 当前 multica 部署状态
- 已上线：模块在 `packages/core/image-compress/`，接线在 `use-file-upload.ts`；镜像 `multica-web:canvas-photo-20260611` 已部署生产，DOCTOR_OK。
- 分支：`fe-photo`（= 画布工作台线 + 本模块）。**注意：本储备包独立于该分支，不随分支变动。**

## 12. 独立性铁规(苏总 2026-06-12)
- **照片=独立文件包,家在主干 `main`**(commit 29a0bcac)。**绝不把照片贴进画布或任何功能分支**——画布一重建照片就被覆盖(canvas-wf10-photo 是反面教训)。
- 规则:前端构建一律从 main 或 main 基线分支出 → 自动继承照片;画布/其它分支应 merge main 获得照片,不各自 bolt。
- 线上 canvas-wf10-photo 是耦合版(可用),规范是后续让画布线 merge main 继承,逐步收敛。

## 12. 独立性铁规(苏总 2026-06-12)
- **照片 = 独立文件包,家在主干 `main`**(commit 29a0bcac)。**绝不把照片贴进画布或任何功能分支**——画布一重建照片就被覆盖(canvas-wf10-photo 是反面教训)。
- 规则:前端构建一律从 main 或 main 基线分支出 → 自动继承照片;画布/其它分支应 merge main 获得照片,不各自 bolt。
- 线上 canvas-wf10-photo 是耦合版(可用),规范是后续让画布线 merge main 继承,逐步收敛。
