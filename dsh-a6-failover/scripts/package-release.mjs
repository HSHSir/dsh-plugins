import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { inspectFile } from './check-encoding.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
const outDir = path.join(root, 'dist');
const base = `${pkg.name}-v${pkg.version}`;
const staging = path.join(outDir, base);
const zip = path.join(outDir, `${base}.zip`);

const included = [
  'package.json', 'README.md', 'LICENSE', 'client.js', 'cordis.patch.yml',
  'assets/a6-icon.png', 'lib', 'scripts', 'CHANGELOG.md', 'INSTALL.txt',
];

function listFiles(dir) {
  const files = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) files.push(...listFiles(full));
    else files.push(full);
  }
  return files;
}

for (const rel of included) {
  const full = path.join(root, rel);
  if (!fs.existsSync(full)) throw new Error(`发布文件缺失：${rel}`);
  const stat = fs.statSync(full);
  const files = stat.isDirectory() ? listFiles(full) : [full];
  for (const file of files) {
    if (/\.(js|mjs|cjs|json|md|ya?ml)$/.test(file)) {
      const result = inspectFile(file);
      if (!result.ok) throw new Error(`编码检查失败：${path.relative(root, file)} (${result.issues.join(', ')})`);
    }
  }
}

if (fs.existsSync(staging)) fs.rmSync(staging, { recursive: true, force: true });
if (fs.existsSync(zip)) fs.rmSync(zip, { force: true });
fs.mkdirSync(staging, { recursive: true });

for (const rel of included) {
  const src = path.join(root, rel);
  const dst = path.join(staging, rel);
  fs.mkdirSync(path.dirname(dst), { recursive: true });
  fs.cpSync(src, dst, { recursive: true });
}

fs.writeFileSync(path.join(staging, 'INSTALL.txt'), `dsh-a6-failover v${pkg.version}\n\n安装：\n1. 解压此 ZIP。\n2. 在解压目录打开终端并运行：node scripts/install.mjs\n3. 完全退出并重新打开 DSH（包括托盘进程）。\n\n安装 CLI web profile：node scripts/install.mjs --profile web\n卸载：node scripts/install.mjs --uninstall\n\n要求：Windows、DeepSeek Harness、Node.js 18 或更新版本。\n首次安装默认使用 DSH_HOME；未设置时为 %USERPROFILE%\\.dsh。\n图标：assets\\a6-icon.png\n`,'utf8');

const command = `Compress-Archive -Path '${staging}\\*' -DestinationPath '${zip}' -CompressionLevel Optimal`;
const result = spawnSync('powershell.exe', ['-NoProfile', '-Command', command], { encoding: 'utf8' });
if (result.status !== 0) throw new Error(result.stderr || 'Compress-Archive failed');
console.log(`已生成：${zip}`);
console.log(`发布文件：${included.join(', ')}`);
