/**
 * PM2 进程配置 —— 部署到 Ubuntu server（172.20.49.43）用。
 *
 *   启动 / 重载 / 停止：
 *     pm2 start    ecosystem.config.js
 *     pm2 reload   acemate-backend          # 改代码后用这个（零停机）
 *     pm2 stop     acemate-backend
 *     pm2 logs     acemate-backend
 *     pm2 save                              # 把当前进程表固化，配合 pm2 startup 开机自启
 *
 * 为什么是这个形状（每一条都有具体原因，不要"顺手简化"）：
 *
 *   1. instances: 1 + exec_mode: 'fork' —— **不能改成 cluster**。
 *      数据全在 SQLite 单文件里（var/ 下三个库）。cluster 模式下多个进程会同时
 *      持同一批文件，即便配了 busy_timeout 也只是把 `database is locked`
 *      从"必现"变成"偶现"，属于最难查的一类故障。
 *      要提并发只能换库，不是加 worker。
 *
 *   2. watch: false —— **绝对不能开**。
 *      应用自己的 SQLite 就写在 var/ 下，开了 watch 会变成
 *      「写库 → 触发重启 → 写库 → …」的死循环，还会把 WAL 搅坏。
 *
 *   3. kill_timeout: 8000 —— uvicorn 是优雅关闭，PM2 默认只等 1600ms，
 *      等不到就 SIGKILL。正在处理的请求（push 一次最多 500 条）会被硬砍。
 *
 *   4. 环境变量从 deploy/acemate.env 读，**不写在本文件里**。
 *      本文件进 git，而 NETPULSE_INGEST_KEY / WECHAT_SECRET 是口令。
 *
 *   5. 环境文件缺失 → 直接抛错，不兜底。
 *      NETPULSE_INGEST_KEY 为空时，/api/ingest、/api/raw **写接口一律放行**
 *      （见 server/security.py），而应用只在启动日志里打一行告警 ——
 *      在 PM2 的日志海洋里等于看不见。宁可起不来。
 */
const fs = require('fs');
const path = require('path');

const ROOT = __dirname;
const ENV_FILE = process.env.ACEMATE_ENV_FILE || path.join(ROOT, 'deploy', 'acemate.env');

// 对外绑定地址。默认 0.0.0.0（局域网内可直接访问）；
// 只给本机 + Nginx 反代时改成 127.0.0.1。
const BIND_HOST = process.env.ACEMATE_BIND_HOST || '0.0.0.0';
const BIND_PORT = process.env.ACEMATE_BIND_PORT || '8787';

/** 解析极简 KEY=VALUE 环境文件（不引入 dotenv 依赖）。 */
function parseEnvFile(file) {
  const out = {};
  const text = fs.readFileSync(file, 'utf8');
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq < 1) continue;
    const key = line.slice(0, eq).trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(key)) continue;
    let value = line.slice(eq + 1).trim();
    // 成对引号是给人看的，值里不带
    if (value.length >= 2 && (value[0] === '"' || value[0] === "'") && value[value.length - 1] === value[0]) {
      value = value.slice(1, -1);
    }
    out[key] = value;
  }
  return out;
}

if (!fs.existsSync(ENV_FILE)) {
  throw new Error(
    '\n[ecosystem] 缺少环境文件：' + ENV_FILE + '\n' +
    '  先执行： cp deploy/acemate.env.example deploy/acemate.env   然后按注释填。\n' +
    '  这里刻意不做兜底：不配 NETPULSE_INGEST_KEY 时采集写口是完全敞开的，\n' +
    '  而那种状态下应用照常启动、只打一行告警 —— 起不来比"悄悄开着"安全。\n'
  );
}

const appEnv = parseEnvFile(ENV_FILE);

// 只在**真的对外暴露**时才强制要求采集口令。
// 绑 127.0.0.1 时外部到不了那个端口，空口令只是开发期的便利。
const EXPOSED = !['127.0.0.1', 'localhost', '::1'].includes(BIND_HOST);
if (EXPOSED && !appEnv.NETPULSE_INGEST_KEY) {
  throw new Error(
    '\n[ecosystem] 绑定在 ' + BIND_HOST + '（对外暴露），但 NETPULSE_INGEST_KEY 为空。\n' +
    '  此时 /api/ingest、/api/raw 的写接口无需任何口令即可调用，任何人都能往库里灌数据。\n' +
    '  生成一个再填进 deploy/acemate.env：\n' +
    '    openssl rand -hex 24\n' +
    '  或者把 ACEMATE_BIND_HOST 设为 127.0.0.1，只让 Nginx 反代进来。\n'
  );
}

module.exports = {
  apps: [
    {
      name: 'acemate-backend',
      cwd: ROOT,

      // 走 run.py 而不是直接 uvicorn：它启动前会检查依赖与
      // web/assets/css/app.css 是否存在，缺什么就打印**能直接复制**的修复命令。
      // 直接跑 uvicorn 的话，缺 CSS 只会表现为"页面没样式"，不报错。
      script: 'run.py',
      interpreter: process.env.ACEMATE_PYTHON || path.join(ROOT, '.venv', 'bin', 'python3'),
      args: `--host ${BIND_HOST} --port ${BIND_PORT}`,

      instances: 1,
      exec_mode: 'fork',
      watch: false,

      autorestart: true,
      restart_delay: 2000,
      max_restarts: 10,
      max_memory_restart: '600M',
      kill_timeout: 8000,
      listen_timeout: 10000,

      out_file: path.join(ROOT, 'var', 'log', 'acemate.out.log'),
      error_file: path.join(ROOT, 'var', 'log', 'acemate.err.log'),
      merge_logs: true,
      time: true,

      env: Object.assign(
        {
          // 不加这个，Python 的 stdout 会被块缓冲，pm2 logs 要等很久才出内容
          PYTHONUNBUFFERED: '1',
        },
        appEnv
      ),
    },
  ],
};
