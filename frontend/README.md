# 启学聊天前端 v1.0

按 keyan 后端接口制作的简洁聊天网页，采用类似豆包的侧栏、欢迎页和底部输入框，适配电脑和手机。后端仓库：https://github.com/haohaozi328-arch/keyan

## 最快启动

1. 先启动后端所需的 MySQL、Redis 和 FastAPI 服务，确认后端运行在 `http://127.0.0.1:8000`。
2. 在仓库的 `frontend` 目录双击 `start.cmd`。需要 Node.js 20 或更新版本；安装过 Codex 的电脑也会尝试寻找它自带的 Node。
3. 浏览器打开 `http://127.0.0.1:4318`。保留启动窗口，关闭窗口即停止前端服务。

不需要安装前端依赖。不要直接双击 index.html，也不要把它当作普通静态文件单独部署：本地服务负责把网页请求转发给后端。

## 指定后端与验证码配置

在仓库的 `frontend` 文件夹内打开 PowerShell，运行：

```powershell
$env:BACKEND_URL = 'http://127.0.0.1:8000'
$env:BACKEND_ENV_FILE = (Resolve-Path '..\.env').Path
.\start.cmd
```

`BACKEND_URL` 只填服务地址，不加 `/api/v1`。后端地址变更时重启前端。端口冲突可先设置 `$env:FRONTEND_PORT = '4319'`。

`BACKEND_ENV_FILE` 指向现有后端配置文件；前端服务只从中读取验证码的公开 VID 和启用状态，VKEY 和模型密钥不会发给浏览器。未指定时查找前端目录上一级的 `.env`。请保留后端配置文件在原处，交付包不包含它。

若本机没有系统 Node，但安装过 Codex，也可尝试它的内置运行时：

```powershell
& "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe" .\start.cjs
```

## 注册与 HTTPS

后端启用 VAPTCHA 时，发送注册邮箱验证码需要完成真实人机验证。该流程需要 HTTPS 页面、有效的 VAPTCHA 配置和后端邮件服务。已有账号可以在本机 HTTP 页面登录。

使用浏览器信任、且包含 `127.0.0.1` 的本地证书后，在同一个 PowerShell 窗口设置证书路径，再启动：

```powershell
$env:TLS_CERT = 'C:\certs\localhost.pem'
$env:TLS_KEY = 'C:\certs\localhost-key.pem'
.\start.cmd
```

此时访问 `https://127.0.0.1:4318`。证书需自行准备并正确受信任，同时确认 VAPTCHA 控制台允许当前访问域名。不要跳过证书校验或把私钥放进交付包。

## 已包含

- 登录、邮箱注册、令牌刷新、退出。
- 流式聊天、断线续传、停止接收、继续接收。
- 历史会话及消息分页、会话改名和删除。
- Markdown 与代码块显示、复制回答或代码、导出 Markdown、回答赞踩。
- 四个学习提问入口、逐步提示开关、后端连接状态。

登录状态仅存于当前浏览器标签页的 sessionStorage。历史内容保存在后端；登录同一账号后可重新加载。

“逐步提示”会在请求中附加学习方式说明，实际效果取决于后端模型，需要真实测试。“停止接收”只中断本地读取，后端仍可能短暂生成，不等于立即停止模型计费。续传窗口过期后，请重新打开历史会话查看已保存内容。

当前没有文件上传、联网搜索、知识库检索或模型切换，这些需要后端提供相应能力。此启动方式仅监听本机，尚未做公网部署。

## 接下来做什么

先让后端运行起来，用真实账号完成一次“登录 → 提问 → 收到完整回答 → 重开历史 → 赞踩”的流程；再验证邮件注册和 VAPTCHA。通过后再请队友试用、记录问题，并安排服务器部署。验证范围见同目录的《验证记录.md》。

## 开发与测试

运行前端不需要 npm 安装依赖。修改后要执行自动检查时，在 `frontend` 目录运行：

```powershell
npm ci --ignore-scripts --no-audit --no-fund
npm run check
npm test
```

测试使用 Node 内置测试工具和 jsdom，覆盖接口客户端、本地代理和页面交互。所有账号、接口回答都为测试夹具，不会调用真实模型、发送邮件或接触后端数据库。本次在 Windows、Node 20 和 24 下执行通过；仓库尚未配置自动运行这些测试的 GitHub Actions。
