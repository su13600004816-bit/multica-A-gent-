/**
 * 图片压缩工具
 * 优化图片到 Anthropic Vision 最优尺寸，降低发给主脑的 token
 *
 * 压缩参数:
 * - 长边 ≤1568px (Anthropic vision 最优尺寸)
 * - 输出 WebP，质量 ~0.8
 * - 压缩失败、非 WebP、超 2MB 或长边超限时阻止图片上传
 */

import { IMAGE_COMPRESSION_QUALITY, MAX_IMAGE_LONG_EDGE, MAX_UPLOAD_IMAGE_BYTES } from "./config";

export interface CompressionResult {
  file: File;
  originalSize: number;
  compressedSize: number;
  compressionRatio: number;
  notice: string;
  width: number;
  height: number;
  sourceWidth: number;
  sourceHeight: number;
  resized: boolean;
  outputType: string;
  usedOriginal: boolean;
  reason: 'not_image' | 'webp' | 'original_smaller' | 'webp_unsupported' | 'compressed_too_large' | 'compression_error' | 'invalid_compressed_image';
}

export { IMAGE_COMPRESSION_QUALITY, MAX_IMAGE_LONG_EDGE };

export interface CompressedImageValidation {
  ok: boolean;
  width: number;
  height: number;
  size: number;
  type: string;
  reason: 'ok' | 'not_webp' | 'too_large' | 'long_edge_too_large' | 'decode_error';
}

export async function validateCompressedImage(file: File): Promise<CompressedImageValidation> {
  const result: CompressedImageValidation = {
    ok: false,
    width: 0,
    height: 0,
    size: file.size,
    type: file.type || 'application/octet-stream',
    reason: 'decode_error'
  };

  if (file.type !== 'image/webp') {
    return { ...result, reason: 'not_webp' };
  }

  if (file.size > MAX_UPLOAD_IMAGE_BYTES) {
    return { ...result, reason: 'too_large' };
  }

  let imageBitmap: ImageBitmap | null = null;
  try {
    imageBitmap = await createImageBitmap(file);
    const width = imageBitmap.width;
    const height = imageBitmap.height;
    if (Math.max(width, height) > MAX_IMAGE_LONG_EDGE) {
      return { ...result, width, height, reason: 'long_edge_too_large' };
    }

    return {
      ok: true,
      width,
      height,
      size: file.size,
      type: file.type,
      reason: 'ok'
    };
  } catch {
    return result;
  } finally {
    imageBitmap?.close();
  }
}

function buildBlockedResult(
  file: File,
  originalSize: number,
  compressedSize: number,
  width: number,
  height: number,
  sourceWidth: number,
  sourceHeight: number,
  resized: boolean,
  notice: string,
  reason: CompressionResult['reason']
): CompressionResult {
  return {
    file,
    originalSize,
    compressedSize,
    compressionRatio: originalSize > 0 ? compressedSize / originalSize : 1,
    notice,
    width,
    height,
    sourceWidth,
    sourceHeight,
    resized,
    outputType: file.type || 'application/octet-stream',
    usedOriginal: true,
    reason
  };
}

function isHeicLikeFile(file: File): boolean {
  return file.type === 'image/heic' ||
    file.type === 'image/heif' ||
    /\.(heic|heif)$/i.test(file.name);
}

/**
 * 压缩图片文件
 * @param file 原始文件
 * @returns Promise<CompressionResult> 压缩结果
 */
export async function compressImage(file: File): Promise<CompressionResult> {
  // 非图片文件直接返回
  if (!file.type.startsWith('image/')) {
    return {
      file,
      originalSize: file.size,
      compressedSize: file.size,
      compressionRatio: 1,
      notice: '非图片文件，无需压缩',
      width: 0,
      height: 0,
      sourceWidth: 0,
      sourceHeight: 0,
      resized: false,
      outputType: file.type || 'application/octet-stream',
      usedOriginal: true,
      reason: 'not_image'
    };
  }

  let imageBitmap: ImageBitmap | null = null;

  try {
    const originalSize = file.size;
    const canvas = document.createElement('canvas');
    const ctx = canvas.getContext('2d');

    if (!ctx) {
      throw new Error('无法获取canvas上下文');
    }

    // 使用 createImageBitmap 加载图片（按审计要求）
    imageBitmap = await createImageBitmap(file);

    // 计算压缩后的尺寸，长边不超过 1568px (Anthropic Vision 最优)
    const sourceWidth = imageBitmap.width;
    const sourceHeight = imageBitmap.height;
    let width = sourceWidth;
    let height = sourceHeight;

    let resized = false;
    if (width > MAX_IMAGE_LONG_EDGE || height > MAX_IMAGE_LONG_EDGE) {
      resized = true;
      if (width > height) {
        height = Math.round((height * MAX_IMAGE_LONG_EDGE) / width);
        width = MAX_IMAGE_LONG_EDGE;
      } else {
        width = Math.round((width * MAX_IMAGE_LONG_EDGE) / height);
        height = MAX_IMAGE_LONG_EDGE;
      }
    }

    canvas.width = width;
    canvas.height = height;

    // 绘制压缩后的图片
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(imageBitmap, 0, 0, width, height);

    const qualitySteps = [
      IMAGE_COMPRESSION_QUALITY,
      0.7,
      0.6
    ].filter((quality, index, values) => values.indexOf(quality) === index);
    let sawTooLargeCompressedImage = false;

    for (const quality of qualitySteps) {
      const blob = await new Promise<Blob | null>((resolve) => {
        canvas.toBlob(resolve, 'image/webp', quality);
      });

      if (!blob) {
        continue;
      }

      const compressedSize = blob.size;
      const compressionRatio = compressedSize / originalSize;
      const baseName = file.name.replace(/\.[^.]+$/, '');
      const compressedFile = new File([blob], `${baseName}.webp`, {
        type: 'image/webp',
        lastModified: Date.now()
      });
      const validation = await validateCompressedImage(compressedFile);

      if (!validation.ok) {
        if (validation.reason === 'too_large') {
          sawTooLargeCompressedImage = true;
          continue;
        }
        return buildBlockedResult(
          file,
          originalSize,
          compressedSize,
          width,
          height,
          sourceWidth,
          sourceHeight,
          resized,
          '图片压缩结果未达标，已阻止图片上传',
          'invalid_compressed_image'
        );
      }

      // 只有原图已经是 WebP、未缩放且重压不省体积时，才允许继续使用原图。
      if (!resized && file.type === 'image/webp' && compressedSize >= originalSize) {
        const originalValidation = await validateCompressedImage(file);
        if (!originalValidation.ok) {
          return buildBlockedResult(
            file,
            originalSize,
            originalSize,
            originalValidation.width || width,
            originalValidation.height || height,
            sourceWidth,
            sourceHeight,
            resized,
            '原 WebP 文件未达上传标准，已阻止图片上传',
            'invalid_compressed_image'
          );
        }
        return {
          file,
          originalSize,
          compressedSize: originalSize,
          compressionRatio: 1,
          notice: '压缩后文件未减小，使用原文件',
          width,
          height,
          sourceWidth,
          sourceHeight,
          resized,
          outputType: file.type || 'application/octet-stream',
          usedOriginal: true,
          reason: 'original_smaller'
        };
      }

      // 生成压缩信息
      const originalMB = (originalSize / (1024 * 1024)).toFixed(1);
      const compressedKB = (compressedSize / 1024).toFixed(0);
      const savedPercent = Math.round((1 - compressionRatio) * 100);

      const sizeText = savedPercent > 0
        ? `省${savedPercent}%`
        : '已转 WebP';
      const notice = resized
        ? `压缩完成: ${originalMB}MB→${compressedKB}KB (${sizeText}, ${sourceWidth}×${sourceHeight}→${width}×${height}) WebP`
        : `图片已转 WebP: ${originalMB}MB→${compressedKB}KB (${sizeText})`;

      return {
        file: compressedFile,
        originalSize,
        compressedSize,
        compressionRatio,
        notice,
        width,
        height,
        sourceWidth,
        sourceHeight,
        resized,
        outputType: 'image/webp',
        usedOriginal: false,
        reason: 'webp'
      };
    }

    if (sawTooLargeCompressedImage) {
      return buildBlockedResult(
        file,
        originalSize,
        originalSize,
        width,
        height,
        sourceWidth,
        sourceHeight,
        resized,
        '图片压缩后仍超过2MB，已阻止图片上传',
        'compressed_too_large'
      );
    }

    return buildBlockedResult(
      file,
      originalSize,
      originalSize,
      width,
      height,
      sourceWidth,
      sourceHeight,
      resized,
      '浏览器不支持 WebP 压缩，已阻止图片上传',
      'webp_unsupported'
    );
  } catch (error) {
    console.error('图片压缩过程出错:', error);
    const notice = isHeicLikeFile(file)
      ? '当前浏览器不能解码 HEIC，请在相册中选择兼容格式或先转 JPEG/WebP'
      : '压缩失败，已阻止图片上传';
    return {
      file,
      originalSize: file.size,
      compressedSize: file.size,
      compressionRatio: 1,
      notice,
      width: 0,
      height: 0,
      sourceWidth: 0,
      sourceHeight: 0,
      resized: false,
      outputType: file.type || 'application/octet-stream',
      usedOriginal: true,
      reason: 'compression_error'
    };
  } finally {
    imageBitmap?.close();
  }
}

/**
 * 批量压缩图片文件
 * @param files 文件列表
 * @returns Promise<CompressionResult[]> 批量压缩结果
 */
export async function compressImages(files: FileList | File[]): Promise<CompressionResult[]> {
  const fileArray = Array.from(files);
  const compressionPromises = fileArray.map(file => compressImage(file));
  return Promise.all(compressionPromises);
}

/**
 * 检查文件是否为图片
 * @param file 文件
 * @returns boolean 是否为图片
 */
export function isImageFile(file: File): boolean {
  return file.type.startsWith('image/');
}

/**
 * 格式化文件大小显示
 * @param bytes 字节数
 * @returns string 格式化后的大小字符串
 */
export function formatFileSize(bytes: number): string {
  if (bytes === 0) return '0 B';

  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));

  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
}
