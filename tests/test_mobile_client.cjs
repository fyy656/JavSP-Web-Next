// Android 客户端依赖的移动模式：入口、登录回跳与精简布局的离线回归。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const web = path.join(__dirname, '../javsp_web/web');
const index = fs.readFileSync(path.join(web, 'index.html'), 'utf8');
const login = fs.readFileSync(path.join(web, 'login.html'), 'utf8');
const app = fs.readFileSync(path.join(web, 'assets/app.js'), 'utf8');
const mobile = fs.readFileSync(path.join(web, 'assets/mobile.css'), 'utf8');

assert.match(index, /assets\/mobile\.css\?v=__ASSET_VERSION__/, 'index 需要加载 mobile.css');
assert.match(index, /javsp-web\.mobile-app/, 'index 需要识别 ?mobile=1');
assert.match(index, /dataset\.initialView\s*=\s*'library'/, '移动模式强制进入媒体库');

assert.match(app, /const MOBILE_APP/, 'app.js 需要移动模式标记');
assert.match(app, /MOBILE_APP \? '\/login\?mobile=1' : '\/login'/, '登录过期要保留移动模式');
assert.match(app, /if \(MOBILE_APP\) \{\s*showView\('library'\)/, '移动模式只打开媒体库');

assert.match(login, /mobileApp \? '\/\?mobile=1' : '\/'/, '登录成功要回到移动模式');
assert.match(mobile, /\.sidebar \{ display: none !important; \}/, '移动模式隐藏侧边栏');
assert.match(mobile, /data-panel\]:not\(\[data-panel="library"\]\)/, '移动模式隐藏其它页面');
assert.match(mobile, /#library-sort-label/, '移动模式隐藏排序');
assert.match(mobile, /\.lib-player-dialog/, '移动模式包含播放器布局');

console.log('Mobile client checks passed.');
