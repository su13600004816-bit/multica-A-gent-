# 技术资产库(tech-assets)

PL1 集团自研核心技术的**唯一归档处**。目的:技术独立建档、配强制说明书、跟当前系统解耦,方便整体迁移到新开发的系统站复用。**不准把技术文档散落到别处。**

## 目录约定
每项技术一个独立子目录,目录内必须有一份 `SPEC.md`(强制说明书),技术相关的可抽取代码/示例/资源也放各自目录内。

```
docs/tech-assets/
├── SPEC_TEMPLATE.md          # 强制说明书模板,新建技术复制它去填
├── README.md                 # 本文件
├── image-compression/        # ① 上传照片压缩 (T01 · PL-151)
│   └── SPEC.md
├── canvas-orchestration/     # ② 画布编排     (T02 · PL-152)
│   └── SPEC.md
├── watchdog/                 # ③ 看门狗       (T03 · PL-153)
│   └── SPEC.md
└── memory-store/             # ④ 记忆储存     (T03 · PL-154)
    └── SPEC.md
```

## 建档流程(四条线统一照此)
1. 复制 `SPEC_TEMPLATE.md` 到本技术目录下改名 `SPEC.md`。
2. 逐项填空:占位 `【】` 全部填掉,源码路径必须真实存在(审核逐条点开核对)。
3. 涉及代码抽取的,改完 `tsc` + `build` 必须过;commit 并在对应任务回报 commit/PR。
4. 完工置 in_review,审计/cc 验过(真路径、真构建)才 done。

## 谷歌同步存档(找得回 / 用得上)
本目录是单一可信源,经 GitHub 已天然云端可恢复。另在集团谷歌盘做镜像备份:

- 远端:`gdrive:PL1集团成品库/技术资产/`(rclone 已配 `gdrive:` 远端)
- 同步:`rclone sync docs/tech-assets/ "gdrive:PL1集团成品库/技术资产/" --create-empty-src-dirs`
- 四条线交付过审后,由 cc 统一执行同步,保证谷歌盘与仓库一致。
