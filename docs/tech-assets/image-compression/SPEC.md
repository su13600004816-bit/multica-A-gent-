<!--
强制说明书 · 归属总任务 PL-150 / 子任务 PL-151
铁律:① 每个占位都要填,不适用写"不适用:原因"。
      ② 凡写源码路径,必须经 grep/打开核对、真实存在。
本档结论以 2026-06-11 仓库 multica-A-gent- 基线 (HEAD a0d78bae,叠加 PL-150 模板提交 09905c61) 为准。
-->

# 上传照片压缩 强制说明书(SPEC)

| 字段 | 内容 |
|---|---|
| 技术名称 | 上传照片压缩 / image-compression |
| 归档目录 | `docs/tech-assets/image-compression/` |
| 负责线 | T01 |
| 当前状态 | **无压缩实现**:上传管线已部署生产,但前端/后端均未做任何图片压缩、缩放或重编码 |
| 对应任务 | PL-151 |
| 维护人 | cc 任务总管 / 线小队-T01 |
| 最后更新 | 2026-06-11 |

> ⚠️ **核查结论(铁律:只认100%真结果)**:对全仓库(前端 TS/TSX、移动端 Expo、Go 后端、依赖清单)做了完整 grep 核查,**当前代码库没有任何"上传照片压缩"逻辑**。本说明书如实记录"现状=零压缩的直传管线",并在第 6 节给出迁移到新系统时补齐压缩的解耦点与建议。下文所有路径与行号均经打开核对、真实存在。

## 1. 技术原理与作用
**一句话**:本技术目前**不存在**。系统对用户上传的图片/附件**不做压缩**——客户端选好原图后,仅校验 ≤100MB,即原样(原分辨率、原质量、原格式)经 `multipart/form-data` 直传到后端,后端 `io.ReadAll` 读全量字节后原样写入对象存储(S3/本地)。

为什么要建这份档:① 把"上传照片"这条管线的真实入口、参数、解耦点说清,方便迁移到新系统;② 明确指出"压缩"是**待补齐能力**(当前缺失),迁移时应在管线里增设客户端压缩或后端缩略图生成,本档第 5/6 节给出落点。

仅有的、与"图片优化"沾边但**不作用于用户上传**的两处,均不属于上传压缩,见第 6 节澄清:
- Next.js 内置 `<Image>` 静态资源格式优化(avif/webp,质量 75/80/85)——只作用于打包/营销/文档静态图,不碰用户上传文件。
- 编辑器 `createImageBitmap` 仅读取图片**像素尺寸**用于占位防抖,读完即 `bitmap.close()`,不改像素。

## 2. 核心源码文件清单(真实路径)
> 以下均为上传管线真实文件;标注其是否涉及压缩。结论:无一做压缩。

| 文件路径 | 角色 | 说明 |
|---|---|---|
| `packages/core/api/client.ts` | Web 上传入口 | `uploadFile(file, opts)`(L1538 起):把 `File` 塞进 `FormData` 直接 `POST /api/upload-file`,**无任何图片处理** |
| `packages/core/hooks/use-file-upload.ts` | Web 上传 Hook | `useFileUpload()`(L19 起):L27-29 仅校验 `file.size > MAX_FILE_SIZE` 抛错,然后调 `api.uploadFile`,**无压缩** |
| `packages/core/constants/upload.ts` | 配置常量 | `MAX_FILE_SIZE = 100 * 1024 * 1024`(100MB)——唯一的上传约束,是大小上限,不是压缩 |
| `packages/views/agents/components/avatar-picker.tsx` | 头像上传 UI | L39 用 `useFileUpload(api)`,L138 `accept="image/*"`;直传,**头像不缩放/不压缩** |
| `apps/mobile/components/editor/use-file-attach.ts` | 移动端图片选取 | `pickAndUploadImage()`(L81 起):`ImagePicker.launchImageLibraryAsync({ quality: 1 })`(L84-88)——`quality:1` = picker 取**最高质量**,非压缩;随后直传 |
| `apps/mobile/components/composer/message-composer.tsx` | 移动端消息附图 | L342-344 同样 `launchImageLibraryAsync({ quality: 1 })`,L349 仅 `fileSize > MAX_FILE_SIZE` 校验,L313 `api.uploadFile(asset, ...)` 直传 |
| `apps/mobile/data/api.ts` | 移动端上传入口 | `uploadFile(asset, opts)`(L1187 起):用 `{ uri, name, type }` 组 `FormData` 直接 `POST /api/upload-file`,**无处理** |
| `server/internal/handler/file.go` | 后端上传处理 | `UploadFile()`(L190);L33 `maxUploadSize = 100<<20`;L203 `http.MaxBytesReader`;L236 `io.ReadAll(file)`;L314/L341 `h.Storage.Upload(...)` 原样落盘。**无 image/jpeg、image.Decode、resize 等任何图片处理调用** |
| `packages/views/editor/extensions/file-upload.ts` | (非压缩)尺寸测量 | `readImageDimensions()`(L74-86)用 `createImageBitmap` 只读宽高做占位防抖,读完 `bitmap.close()`,**不改像素**;列此是为澄清它常被误认作压缩 |

**调用关系/数据流**(直传,无压缩节点):
```
[Web]   选文件 → use-file-upload.upload() 校验≤100MB → client.uploadFile() → POST /api/upload-file
[移动端] ImagePicker(quality:1) → use-file-attach/message-composer 校验≤100MB → api.uploadFile() → POST /api/upload-file
                                              │
                          (全程无压缩/缩放/重编码)
                                              ▼
[后端]  file.go UploadFile: MaxBytesReader(100MB) → io.ReadAll(原始字节) → Storage.Upload(原样) → 返回 Attachment(url)
```

## 3. 对外接口 / API / 事件
> 新系统接管"上传"管线时的调用面。当前这些接口都不含压缩参数。

| 接口/函数/事件 | 入参 | 出参 | 说明 |
|---|---|---|---|
| `ApiClient.uploadFile(file, opts?)` (Web, `packages/core/api/client.ts`) | `file: File`;`opts?: {issueId?, commentId?, chatSessionId?}` | `Promise<Attachment>` | 直传 Web 入口;无 quality/maxWidth 等压缩参 |
| `useFileUpload(api, onError?)` (Web Hook) | `api: ApiClient` | `{ upload, uploadWithToast, uploading }` | `upload(file, ctx)` 仅校验大小后转发 |
| `mobileApi.uploadFile(asset, opts?)` (`apps/mobile/data/api.ts`) | `asset:{uri,name,type,size?}` | `Promise<Attachment>` | 移动端直传入口 |
| `pickAndUploadImage(ctx?)` (`use-file-attach.ts`) | `ctx?: UploadContext` | `Promise<FileAttachResult \| null>` | 选图(quality:1,无压缩)+ 直传 |
| `POST /api/upload-file` (`server/.../file.go`) | multipart:`file` + 可选 `issue_id/comment_id/chat_session_id` | JSON `Attachment{url,...}` | 后端唯一上传端点;原样落存储,无服务端图片处理 |

## 4. 依赖项 / 环境变量 / 配置
- **第三方依赖(含版本)**:
  - 移动端选图:`expo-image-picker ~55.0.20`(`apps/mobile/package.json`)。**注意:仓库未引入 `expo-image-manipulator`**(无 `manipulateAsync`/`SaveFormat`),即移动端没有任何缩放/重编码能力。
  - 后端:`server/go.mod` 中**无任何图片库**(无 `golang.org/x/image`、`disintegration/imaging`、`nfnt/resize`、`bimg` 等);仅有 `github.com/klauspost/compress`(通用数据压缩,**非图片**)。
- **环境变量**:上传/存储相关(对象存储 endpoint、bucket、密钥等)由 `Storage` 实现读取,**与压缩无关**(当前无压缩需要的环境变量)。
- **配置项 / 默认值**:`MAX_FILE_SIZE = 100MB`(前端 `packages/core/constants/upload.ts`)与 `maxUploadSize = 100<<20`(后端 `file.go:33`)。无 quality/maxWidth/maxHeight 等压缩配置项。
- **运行前置条件**:可用的对象存储(S3 或本地)+ 后端 `/api/upload-file` 路由。无额外压缩前置。

## 5. 迁移到新系统的步骤(本说明书核心)
> 现状是"零压缩直传"。迁移有两种姿态:**(A) 原样迁直传管线**;**(B) 借迁移补齐压缩能力**。两者都要先搬下列文件并切断耦合点。

1. **要带走的文件**:
   - Web:`packages/core/api/client.ts`(`uploadFile`)、`packages/core/hooks/use-file-upload.ts`、`packages/core/constants/upload.ts`。
   - 移动端:`apps/mobile/data/api.ts`(`uploadFile`)、`apps/mobile/components/editor/use-file-attach.ts`、`apps/mobile/components/composer/message-composer.tsx` 的选图/上传段。
   - 后端:`server/internal/handler/file.go` 的 `UploadFile` 及其 `Storage` 抽象。
2. **依赖当前系统的耦合点(解耦时要切掉/替换)**:
   - 鉴权:`client.ts` 的 `authHeaders()` / 移动端 `Bearer` token + `X-Workspace-Slug`(`getCurrentSlug()`)——绑定本系统鉴权,迁移需替换。
   - 业务外键:`issue_id / comment_id / chat_session_id` 是本系统实体,新系统需映射或去掉。
   - 存储:后端 `h.Storage.Upload(...)` 绑定本系统存储实现与 bucket 约定。
   - 返回结构:`Attachment` 类型(`AttachmentResponseSchema`)是本系统形状。
3. **需要重写的胶水代码**:鉴权头注入、workspace slug 解析、`Attachment` schema 适配、存储 client 适配。
4. **迁移步骤**:复制上述文件 → 替换鉴权/slug/存储胶水 → 对接新系统 `Attachment` schema → 配置存储环境变量 → 跑第 7 节验证。
5. **(B) 若要补齐压缩,推荐落点(当前缺失,迁移是补齐良机)**:
   - 客户端(Web):在 `use-file-upload.ts` 的 `upload()` 内、`api.uploadFile` 之前,对 `image/*` 走 canvas 缩放 + `toBlob(quality)` 重编码(或引入 `browser-image-compression`),产出小 Blob 再传。
   - 移动端:引入 `expo-image-manipulator`,在 `pickAndUploadImage` 选图后 `manipulateAsync(uri, [{resize}], {compress, format})` 再传。
   - 服务端(可选,做缩略图):在 `file.go UploadFile` 落盘前对 `image/*` 用 Go 图片库解码 → resize → 重编码,生成原图 + 缩略图双份。
   - **风险**:务必保持"压缩失败回退原图直传",别因压缩异常阻断上传。
6. **风险/注意**:① 当前 100MB 上限是唯一闸门,大原图会全量上行/落盘,带宽与存储成本高;② 移动端 `quality:1` 是 picker 最高质量,易产出超大图;③ 补压缩时注意 EXIF 方向/透明通道/GIF 动图等格式坑。

## 6. 已知 BUG / 限制 / 坑
> 诚实记录。本技术当前最大的"已知限制"就是:它本身没实现。

| 现象 | 触发条件 | 影响 | 绕法/状态 |
|---|---|---|---|
| **完全无图片压缩** | 任意图片上传(Web/移动端) | 原分辨率/原质量/原格式直传直存,带宽与存储成本高,大图加载慢 | 现状如此;补齐方案见第 5.5 节;**当前唯一约束是 100MB 上限** |
| 移动端 `quality:1` 易产巨图 | `launchImageLibraryAsync({quality:1})` | 现代手机单张原图可达数十 MB | 待引入 `expo-image-manipulator` 压缩;现无绕法 |
| 误以为已有压缩 | 看到 `createImageBitmap`(`file-upload.ts`)/ Next `images.qualities`(`apps/web/next.config.ts:33-36`) | 误判管线已压缩 | **澄清**:前者只读尺寸防抖、读完即 close;后者仅作用于 `<Image>` 静态资源,**都不碰用户上传** |
| 无服务端缩略图 | 列表/预览大量图 | 每次拉原图,前端预览压力大 | 待服务端补缩略图(第 5.5 节);现无 |

## 7. 验证方法
> 怎么证明上述"零压缩"结论为真、且上传管线本身可跑。

- **核查门禁(证明"上传管线无压缩"为真,可复现)**:在仓库根执行,预期**均无上传图片压缩命中**。注意不要用全 `server/` 粗搜 `resize`,当前 `server/internal/daemon/daemon.go` 有 `batch resize` 注释,属于批处理调度语境,不是上传图片压缩实现。
  ```bash
  # 后端上传入口无任何图片解码/缩放/重编码处理
  grep -nE "image/jpeg|image/png|image\\.Decode|jpeg\\.Encode|resize|Thumbnail" server/internal/handler/file.go  # 预期: 空
  grep -niE "golang.org/x/image|disintegration/imaging|nfnt/resize|bimg" server/go.mod                           # 预期: 空
  # 前端/移动端无压缩库与压缩调用
  grep -rn "browser-image-compression\|expo-image-manipulator\|manipulateAsync\|toBlob\|OffscreenCanvas" packages apps --include=*.ts --include=*.tsx | grep -v node_modules  # 预期: 空(createImageBitmap 仅尺寸测量,不在此列)
  ```
- **构建门禁**:本任务仅新增文档(`docs/tech-assets/image-compression/SPEC.md`),**未改动任何 `.ts/.tsx/.go` 源码**,因此 tsc/build 无受影响面;不适用代码抽取构建。若后续按第 5.5 节补压缩代码,再跑 `pnpm -w build` + `tsc`。
- **功能验证(上传管线本身活着)**:登录 web,在 issue 评论里上传一张图 → 应成功返回附件并可预览;用 DevTools Network 看 `POST /api/upload-file` 请求体大小 ≈ 原图大小(印证"无压缩")。
- **新系统可用性验证**:迁过去后,最小用例 = 用新系统鉴权头调 `POST /api/upload-file` 传一张 1MB 图,返回 `Attachment.url` 且能下载回等大文件,即管线存活;若已按 5.5 补压缩,则下载回的文件应小于原图。

---
<!-- 自检:7 节已填;路径/行号均经打开核对真实存在;BUG 节如实写明"零压缩";验证给出可复现 grep。 -->
