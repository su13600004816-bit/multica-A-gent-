// 接入示例：在任意"文件上传入口"调用 compressImage，图片传前压成 ≤1568px WebP。
// 关键：用 result.file —— 非图片/压缩失败时它就是原文件，天然优雅回退，绝不阻断上传。
// multica 实际接线位置：packages/core/hooks/use-file-upload.ts 的 upload 回调。
import { compressImage } from "./index";

export async function uploadWithCompression(
  file: File,
  rawUpload: (f: File) => Promise<unknown>,
) {
  let toUpload = file;
  if (file.type.startsWith("image/")) {
    try {
      const result = await compressImage(file);
      toUpload = result.file; // 压缩后的 WebP，或回退的原文件
    } catch {
      toUpload = file; // 兜底：任何异常都走原文件直传
    }
  }
  return rawUpload(toUpload);
}
