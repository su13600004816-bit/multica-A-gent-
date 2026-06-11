// 自包含图片压缩模块（便携：可整块迁移到未来新站）。
// 入口 compressImage(file)：图片压成 ≤1568px WebP；非图片/压缩失败时返回原文件(usedOriginal=true)，
// 调用方直接用 result.file 即可优雅回退，绝不阻断上传。
export {
  compressImage,
  compressImages,
  validateCompressedImage,
  isImageFile,
  formatFileSize,
  IMAGE_COMPRESSION_QUALITY,
  MAX_IMAGE_LONG_EDGE,
} from "./image-compress";
export type { CompressionResult, CompressedImageValidation } from "./image-compress";
export { MAX_UPLOAD_IMAGE_BYTES, CLAUDE_IMAGE_TYPE } from "./config";
