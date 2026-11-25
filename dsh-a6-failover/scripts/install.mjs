/**
 * 把插件装进某个 DSH profile —— 这次是**按包装**，因为设置页（浏览器半侧）只认裸包名。
 *
 * 做四件事：
 *   1. 把插件拷到 $DSH_HOME/plugins/dsh-a6-failover（运行时副本，随便覆盖）；
 *   2. 在 <profile>/node_modules 里放一个指向它的 junction（放不了就复制一份）；
 *   3. 在 profile 的 package.json 里登记 file: 依赖（下次 pnpm 操作不会被当垃圾清掉）；
 *   4. 在 profile 的 cordis.patch.yml 里插入 `name: dsh-a6-failover` 那段。
 *
 * 用法：
 *   node scripts/install.mjs                 # desktop（桌面版用的就是它）
 *   node scripts/install.mjs --profile web   # web
 *   node scripts/install.mjs --dry-run       # 只看会改什么
 *   node scripts/install.mjs --uninstall     # 撤销
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { inspectFile, scanDir } from './check-encoding.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const sourceRoot = path.resolve(here, '..');
const PLUGIN_ID = 'a6-failover';
const PKG = 'dsh-a6-failover';
const BEGIN = `# >>> ${PLUGIN_ID} (installed by scripts/install.mjs — 删这段即可卸载)`;
const END = `# <<< ${PLUGIN_ID}`;
const DEP_SPEC = `file:../../plugins/${PKG}`;

function parseArgs(argv) {
  const out = { profile: '', dryRun: false, uninstall: false };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--dry-run') out.dryRun = true;
    else if (arg === '--uninstall') out.uninstall = true;
    else if (arg === '--profile') out.profile = String(argv[i + 1] || '');
    else if (arg.startsWith('--profile=')) out.profile = arg.slice('--profile='.length);
  }
  return out;
}

const dshHome = () => String(process.env.DSH_HOME || '').trim() || path.join(os.homedir(), '.dsh');

function existingProfiles(home) {
  const dir = path.join(home, 'profiles');
  if (!fs.existsSync(dir)) return [];
  return fs.readdirSync(dir, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && !entry.name.startsWith('.'))
    .filter((entry) => fs.existsSync(path.join(dir, entry.name, 'package.json')))
    .map((entry) => entry.name);
}

const RUNTIME_FILES = [
  'package.json', 'README.md', 'LICENSE', 'client.js', 'cordis.patch.yml',
  'assets/a6-icon.png', 'lib',
];

function copyDir(from, to) {
  fs.mkdirSync(to, { recursive: true });
  for (const entry of fs.readdirSync(from, { withFileTypes: true })) {
    const src = path.join(from, entry.name);
    const dst = path.join(to, entry.name);
    if (entry.isDirectory()) copyDir(src, dst);
    else fs.copyFileSync(src, dst);
  }
}

function copyRuntime(from, to) {
  fs.mkdirSync(to, { recursive: true });
  for (const rel of RUNTIME_FILES) {
    const src = path.join(from, rel);
    const dst = path.join(to, rel);
    if (!fs.existsSync(src)) throw new Error(`运行文件缺失：${rel}`);
    if (fs.statSync(src).isDirectory()) copyDir(src, dst);
    else {
      fs.mkdirSync(path.dirname(dst), { recursive: true });
      fs.copyFileSync(src, dst);
    }
  }
}

function removePath(target) {
  if (!fs.existsSync(target) && !isLink(target)) return;
  try { fs.rmSync(target, { recursive: true, force: true }); } catch { /* 链接坏了就算了 */ }
}

function isLink(target) {
  try { return fs.lstatSync(target).isSymbolicLink(); } catch { return false; }
}

/** 放一个 junction 指向插件目录；Windows 上不需要管理员权限。 */
function linkInto(from, to) {
  removePath(to);
  try {
    fs.symlinkSync(from, to, 'junction');
    return 'junction';
  } catch {
    copyDir(from, to);
    return 'copy';
  }
}

const BLOCK = `${BEGIN}
- insert:
    - id: ${PLUGIN_ID}
      # 必须是裸包名：设置页（浏览器半侧）只挂在包名行上
      name: ${PKG}
      config:
        enabled: true
        providerPattern: '^a6api'
        defaultControlBase: https://a6api.vip
        # 想让它「切不动就换另一张令牌」时，加一条备用路由：
        # fallbackRoutes:
        #   - provider: a6api-a6-2
        #     model: deepseek-v4.1-flash-1017
${END}`;

function bodyOf(text) {
  return text
    .split('\n')
    .map((line) => line.replace(/(^|\s)#.*$/, ''))
    .filter((line) => line.trim().length > 0)
    .join('\n')
    .trim();
}

export function removeBlock(text) {
  let next = text;
  for (;;) {
    const begin = next.indexOf(BEGIN);
    if (begin < 0) break;
    const end = next.indexOf(END, begin);
    next = `${next.slice(0, begin)}${end >= 0 ? next.slice(end + END.length).replace(/^\n/, '') : ''}`;
  }
  // 老版本（file 路径行、无标记）写的段：一并清掉
  const lines = next.split('\n');
  const out = [];
  for (let i = 0; i < lines.length; i += 1) {
    if (/^\s*- id:\s*a6-failover\s*$/.test(lines[i])) {
      while (out.length && /^\s*(#.*|- insert:\s*)$/.test(out[out.length - 1])) out.pop();
      while (i + 1 < lines.length && /^\s{2,}/.test(lines[i + 1])) i += 1;
      continue;
    }
    out.push(lines[i]);
  }
  next = out.join('\n').replace(/\n{3,}/g, '\n\n');
  return bodyOf(next) === '' ? '[]\n' : next;
}

export function addBlock(text) {
  const cleaned = removeBlock(text);
  const body = bodyOf(cleaned);
  if (body === '' || body === '[]') {
    const replaced = cleaned.replace(/^[ \t]*\[\][ \t]*$/m, BLOCK);
    if (replaced !== cleaned) return replaced.endsWith('\n') ? replaced : `${replaced}\n`;
    return `${BLOCK}\n`;
  }
  return `${cleaned.replace(/\s*$/, '')}\n\n${BLOCK}\n`;
}

function patchPackageJson(profileDir, { add }) {
  const file = path.join(profileDir, 'package.json');
  if (!fs.existsSync(file)) return null;
  const raw = fs.readFileSync(file, 'utf8');
  let json;
  try { json = JSON.parse(raw); } catch { return 'package.json 不是合法 JSON，跳过（不影响插件运行）'; }
  json.dependencies = json.dependencies && typeof json.dependencies === 'object' ? json.dependencies : {};
  const had = Object.prototype.hasOwnProperty.call(json.dependencies, PKG);
  if (add) {
    if (had && json.dependencies[PKG] === DEP_SPEC) return null;
    json.dependencies[PKG] = DEP_SPEC;
  } else {
    if (!had) return null;
    delete json.dependencies[PKG];
  }
  const backup = `${file}.bak-${Date.now()}`;
  fs.copyFileSync(file, backup);
  fs.writeFileSync(file, `${JSON.stringify(json, null, 2)}\n`, 'utf8');
  return `${add ? '已登记' : '已移除'}依赖 ${PKG}（备份：${path.basename(backup)}）`;
}

const main = () => {
  const args = parseArgs(process.argv.slice(2));
  const home = dshHome();

  if (!fs.existsSync(path.join(home, 'profiles'))) {
    console.error(`找不到 DSH_HOME：${home}\\profiles。先打开一次 DeepSeek Harness 让它建目录。`);
    process.exit(1);
  }

  const profiles = existingProfiles(home);
  const profile = args.profile || (profiles.includes('desktop') ? 'desktop' : profiles[0] || '');
  if (!profile || !profiles.includes(profile)) {
    console.error(`没有这个 profile：${profile || '(空)'}。现有：${profiles.join(', ') || '（无）'}`);
    process.exit(1);
  }

  const profileDir = path.join(home, 'profiles', profile);
  const patchFile = path.join(profileDir, 'cordis.patch.yml');
  const pluginDir = path.join(home, 'plugins', PKG);
  const linkPath = path.join(profileDir, 'node_modules', PKG);

  const patchText = fs.existsSync(patchFile) ? fs.readFileSync(patchFile, 'utf8') : '[]\n';

  if (args.uninstall) {
    console.log(`目标：${home} / profile=${profile}`);
    if (!args.dryRun) {
      if (patchText.includes(PLUGIN_ID)) {
        const backup = `${patchFile}.bak-${Date.now()}`;
        fs.copyFileSync(patchFile, backup);
        fs.writeFileSync(patchFile, removeBlock(patchText), 'utf8');
        console.log(`已移除 patch 段（备份：${path.basename(backup)}）`);
      } else {
        console.log('patch 里没有 a6-failover 段。');
      }
      console.log(patchPackageJson(profileDir, { add: false }) || 'package.json 里没有该依赖。');
      removePath(linkPath);
      console.log(`已移除 ${linkPath}`);
      removePath(pluginDir);
      console.log(`已删除插件目录 ${pluginDir}`);
    }
    console.log('\n重启 DSH 生效。');
    return;
  }

  console.log(`目标：${home} / profile=${profile}`);
  console.log(`运行时代码：${pluginDir}`);
  console.log(`profile 内的包位置：${linkPath}`);
  if (args.dryRun) console.log('（dry-run：不写任何文件）');

  if (!args.dryRun) {
    const encodingIssues = RUNTIME_FILES
      .flatMap((rel) => {
        const target = path.join(sourceRoot, rel);
        if (!fs.existsSync(target)) return [];
        if (fs.statSync(target).isDirectory()) return scanDir(target);
        if (!/\.(js|mjs|cjs|json|md|ya?ml)$/.test(target)) return [];
        const info = inspectFile(target);
        return info.ok ? [] : [info];
      });
    if (encodingIssues.length > 0) {
      console.error(`源文件编码检查失败：${encodingIssues.map((item) => path.relative(sourceRoot, item.path)).join(', ')}`);
      console.error('请先把这些文件保存为 UTF-8 无 BOM，再重新安装。');
      process.exit(1);
    }
    removePath(pluginDir);
    copyDir(sourceRoot, pluginDir);
    console.log('已拷贝插件文件。');

    const mode = linkInto(pluginDir, linkPath);
    console.log(mode === 'junction'
      ? `已在 node_modules 里建立 junction → ${pluginDir}`
      : `node_modules 里的 junction 建不了，改成复制一份（注意：以后 pnpm 操作可能清掉，重跑本脚本即可）`);

    console.log(patchPackageJson(profileDir, { add: true }) || `package.json 已登记过 ${PKG}。`);

    if (fs.existsSync(patchFile)) {
      const backup = `${patchFile}.bak-${Date.now()}`;
      fs.copyFileSync(patchFile, backup);
      console.log(`已备份 patch：${path.basename(backup)}`);
    }
    fs.writeFileSync(patchFile, addBlock(patchText), 'utf8');
    console.log('已写入 patch 段（name: dsh-a6-failover）。');
  }

  console.log(`
下一步：
  1. 完全退出 DeepSeek Harness（托盘也退），再打开；
  2. 打开「设置」——左侧会多出一栏「A6 换商家」（如果没出现，说明浏览器半侧没被加载）；
  3. 也可以用命令行确认宿主半侧在跑：
       Invoke-RestMethod http://127.0.0.1:<端口>/a6-failover/status

临时停用：把那段里的 enabled 改成 false；彻底卸载：
  node scripts/install.mjs --profile ${profile} --uninstall
`);
};

const isMain = process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url));
if (isMain) main();
