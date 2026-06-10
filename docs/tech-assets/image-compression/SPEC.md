# 上传照片压缩 强制说明书(SPEC)

| 字段 | 内容 |
|---|---|
| 技术名称 | 上传照片压缩 / image-compression |
| 归档目录 | `docs/tech-assets/image-compression/` |
| 负责线 | T01(本份由 cc 亲自整理) |
| 当前状态 | **已部署在老 canvas 站(canvas.pl-1.com),尚未并入 multica** |
| 对应任务 | PL-151 |
| 维护人 | cc |
| 最后更新 | 2026-06-11 |

> ⚠️ **关键事实(亲自核对)**:multica-A-gent- 仓库内**没有任何图片压缩实现**——其上传链路是原图直传(前端 100MB 上限,后端 `server/internal/handler/file.go` 无压缩/缩放/转码)。真正的压缩技术在**老 canvas 站代码库**(磁盘 `/home/fleet/canvas`,工程名 `ouroboros-circuit-console`)。本 SPEC 据老 canvas 源码编写,目标是把它**迁移进 multica**。

## 1. 技术原理与作用
把用户上传的图片在**前端浏览器内**压缩成 Anthropic Vision 最优尺寸,降低发给主脑(Claude)的 token 与带宽:
- 长边缩放到 ≤ **1568px**(Anthropic vision 最优)
- 输出 **WebP**,质量 **0.8**
- 压缩后做强校验:**非 WebP / 超 2MB / 长边超限 / 压缩失败 → 阻止上传**(宁可拦下,不传坏图)

## 2. 核心源码文件清单(真实路径 · 源在 /home/fleet/canvas)
| 文件路径(老 canvas 仓库相对) | 角色 | 说明 |
|---|---|---|
| `src/lib/imageCompress.ts` | 入口/核心 | `compressImage()` 压缩主逻辑 + `validateCompressedImage()` 校验;`CompressionResult` 结果结构 |
| `src/lib/uploadConfig.ts` | 配置 | 所有阈值常量与类型白名单(见第4节) |
| `src/components/ChatPanel.tsx` | 调用点 | 聊天面板选图/粘贴 → 调压缩 → 上传 |
| `src/app/line-orchestrator/LineOrchestratorConsole.tsx` | 调用点 | 编排台上传入口 |
| `convert_to_webp.py` | 旁证/工具 | Python 版同逻辑(PIL,1568 长边 + WebP q80),可作迁移对照 |

**调用链**:用户选图/粘贴(ChatPanel)→ `compressImage(file)`:`createImageBitmap` 解码 → 按长边 1568 等比缩放绘到 canvas → `toBlob('image/webp', 0.8)` → `validateCompressedImage()`(WebP? ≤2MB? 长边≤1568?)→ 通过则上传 WebP,否则按 `reason` 阻止并提示。

## 3. 对外接口
| 函数 | 入参 | 出参 | 说明 |
|---|---|---|---|
| `compressImage(file: File)` | 原始 File | `Promise<CompressionResult>` | 含 file/原始大小/压缩后大小/压缩比/宽高/是否缩放/outputType/usedOriginal/reason |
| `validateCompressedImage(file: File)` | 压缩后 File | `Promise<CompressedImageValidation>` | `reason: ok\|not_webp\|too_large\|long_edge_too_large\|decode_error` |
| `isAllowedClaudeImageType(type)` | mime | bool | 仅放行 `image/webp` |

## 4. 依赖项 / 环境变量 / 配置
- **依赖**:无第三方压缩库,纯浏览器 API(`createImageBitmap` / `<canvas>.toBlob` / `OffscreenCanvas`);Python 旁证用 `PIL`。
- **配置常量**(`src/lib/uploadConfig.ts`):
  - `MAX_IMAGE_LONG_EDGE = 1568`
  - `MAX_UPLOAD_IMAGE_BYTES = 2*1024*1024`(2MB)
  - `MAX_UPLOAD_FILE_BYTES = 5*1024*1024`(5MB,非图片附件)
  - `IMAGE_COMPRESSION_QUALITY = 0.8`
  - `CLAUDE_IMAGE_TYPE = "image/webp"`;`ALLOWED_IMAGE_TYPES = [webp]`;`ALLOWED_FILE_EXTENSIONS = [.txt,.md,.json,.csv,.log]`
  - `UPLOADS_DIR = "/home/fleet/work/uploads"`(老站服务端落盘目录)

## 5. 迁移到新系统(multica)的步骤 —— 本说明书核心
multica 现状是原图直传、无压缩。迁移=把前端压缩这层补进去:
1. **带走的文件**:`src/lib/imageCompress.ts`、`src/lib/uploadConfig.ts`(阈值常量)。
2. **multica 落点**:压缩逻辑放 `packages/core/`(纯函数无 UI 依赖),阈值合并进 `packages/core/constants/upload.ts`(现有 `MAX_FILE_SIZE=100MB`,新增图片专用 1568/2MB/q0.8/webp)。
3. **接入调用点**:在 multica 上传入口前置压缩——`packages/core/hooks/use-file-upload.ts` 的 `upload()` 里,对 image/* 先 `compressImage()` 再走 `api.uploadFile()`;编辑器路径在 `packages/views/editor/extensions/file-upload.ts` 的 `uploadAndInsertFile()` 内同样前置。
4. **需重写的胶水**:老站直接拿 File 上传;multica 走 FormData + `/api/upload-file`。把压缩产物(WebP File)喂给现有 `api.uploadFile()` 即可,后端不用改(它本来就存原始字节)。移动端 `apps/mobile/components/editor/use-file-attach.ts` 需单独适配(RN 无 canvas,需用 expo-image-manipulator 等替代,**这是迁移最大的坑**)。
5. **风险/坑**:① RN/移动端没有浏览器 canvas,Web 版压缩不能直接复用;② HEIC/HEIF 解码浏览器支持不一(老站有 `isHeicLikeFile` 分支);③ 后端校验要同步收紧(否则压缩可被绕过仍传原图)。

## 6. 已知 BUG / 限制 / 坑
| 现象 | 触发条件 | 影响 | 绕法/状态 |
|---|---|---|---|
| 浏览器不支持 WebP 编码 | 老旧浏览器 | `reason=webp_unsupported`,阻止上传 | 设计如此,提示用户换浏览器 |
| 压缩后仍 >2MB | 超大/高熵图 | `reason=compressed_too_large`,阻止 | 设计如此 |
| HEIC 无法解码 | iPhone 原图 | 可能 decode_error | 老站有 HEIC 分支,迁移需保留 |
| **multica 当前完全无压缩** | 现状 | 大图原样直传、token 浪费 | 本任务迁移后解决 |

## 7. 验证方法
- **老站现状(已验)**:`/home/fleet/canvas/QA_*_hk-imgcompress_*.md` 一批真机 QA 报告(2026-06-07 收口)。
- **迁移后验证(multica)**:① `pnpm tsc` + `build` 过;② 浏览器上传一张 >1568px 大 PNG,DevTools 看实际上传请求 body 为 `image/webp` 且 ≤2MB、长边 ≤1568;③ 上传一张压不下去的图,确认被拦截并有提示;④ 移动端单独验。
