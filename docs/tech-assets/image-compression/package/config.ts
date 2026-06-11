// 图片压缩配置（迁移自老站 ouroboros-circuit-console: src/lib/uploadConfig.ts，仅保留压缩相关常量）。
// 目的：把上传图片压到 Anthropic Vision 最优尺寸，降低发给主脑的 token + 减小上传体积。
export const MAX_IMAGE_LONG_EDGE = 1568; // Anthropic Vision 最优长边(px)
export const MAX_UPLOAD_IMAGE_BYTES = 2 * 1024 * 1024; // 压缩后图片上限 2MB
export const IMAGE_COMPRESSION_QUALITY = 0.8; // WebP 初始质量
export const CLAUDE_IMAGE_TYPE = "image/webp";
