/**
 * 找出插件里所有「中文被 GBK 毁过」的文件。
 *
 * 判据（任一命中即算）：
 *   1. 含 U+FFFD —— 字节已经丢了，救不回来，只能重写；
 *   2. 含私用区字符 U+E000–U+F8FF —— GBK 双字节被当 UTF-8 读时的典型产物；
 *   3. 出现「连续两个以上」mojibake 高频字 —— 单个字可能碰巧是正常中文，
 *      连排才一定是乱码（正常中文里这几个字几乎不会相邻）；
 *   4. 有 UTF-8 BOM —— PowerShell 5 写文件留下的痕迹。
 *
 * 特征字全部用 \u 转义写，这样本文件自己不会被判成乱码。
 *
 * 用法：node scripts/check-encoding.mjs [--strict]
 *   --strict：有问题就退出码 1，供 install.mjs 当门禁。
 */
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");

/** 高置信度 mojibake 特征字（锛 銆 鈥 鐨 鏄 鍦 鎹 瀹 鐗 湰 鏃 硶 缁 鍥 畾 …）。 */
const MOJIBAKE_CHARS = [
  0x951b, 0x9286, 0x9225, 0x9428, 0x93c4, 0x9366, 0x93b9,
  0x7039, 0x9417, 0x6e70, 0x93c3, 0x7876, 0x7f01, 0x93c2, 0x9365,
  0x93bb, 0x93c5, 0x7e3a, 0x7b1a, 0x93ac,
].map((code) => String.fromCodePoint(code)).join("");

const RUN = new RegExp(`[${MOJIBAKE_CHARS}]{2,}`);
const PRIVATE_USE = /[\uE000-\uF8FF]/;
const EXT = /\.(js|mjs|cjs|json|md|ya?ml)$/;

export function inspectFile(full) {
  const buf = fs.readFileSync(full);
  const text = buf.toString("utf8");
  const bom = buf[0] === 0xef && buf[1] === 0xbb && buf[2] === 0xbf;
  const replacement = text.includes("\uFFFD");
  const privateUse = PRIVATE_USE.test(text);
  const run = RUN.exec(text);
  const issues = [];
  if (bom) issues.push("BOM");
  if (replacement) issues.push("丢字节");
  if (privateUse) issues.push("私用区");
  if (run) issues.push(`乱码串「${run[0].slice(0, 6)}」`);
  return { path: full, bytes: buf.length, ok: issues.length === 0, issues };
}

export function scanDir(root) {
  const hits = [];
  if (!fs.existsSync(root)) return hits;
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    if (entry.name === "node_modules" || entry.name === ".git") continue;
    const full = path.join(root, entry.name);
    if (entry.isDirectory()) {
      hits.push(...scanDir(full));
      continue;
    }
    if (!EXT.test(entry.name)) continue;
    const info = inspectFile(full);
    if (!info.ok) hits.push(info);
  }
  return hits;
}

const invokedDirectly = process.argv[1]
  && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));

if (invokedDirectly) {
  const strict = process.argv.includes("--strict");
  const targets = [
    ["开发副本", ROOT],
    ["已安装", path.join(os.homedir(), ".dsh", "plugins", "dsh-a6-failover")],
  ];
  let total = 0;
  for (const [label, root] of targets) {
    const hits = scanDir(root);
    total += hits.length;
    console.log(`${label} ${root}: ${hits.length} 个有问题`);
    for (const hit of hits) {
      console.log(`  x ${path.relative(root, hit.path)}  [${hit.issues.join(" / ")}]  ${hit.bytes} 字节`);
    }
  }
  if (strict && total > 0) {
    console.error(`\n编码门禁未通过：${total} 个文件有问题`);
    process.exit(1);
  }
  if (total === 0) console.log("OK 全部干净（UTF-8 无 BOM，无乱码）");
}
