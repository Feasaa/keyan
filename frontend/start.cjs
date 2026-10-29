const { spawn } = require('node:child_process');
const fs = require('node:fs');
const { createServer } = require('./server.cjs');
const tls = process.env.TLS_CERT && process.env.TLS_KEY ? { cert: fs.readFileSync(process.env.TLS_CERT), key: fs.readFileSync(process.env.TLS_KEY) } : undefined;
const port = Number(process.env.FRONTEND_PORT || 4318), url = `${tls ? 'https' : 'http'}://127.0.0.1:${port}`;
createServer({ tls }).listen(port, '127.0.0.1', () => {
  console.log(`\nQixue: ${url}\nBackend: ${process.env.BACKEND_URL || 'http://127.0.0.1:8000'}\nKeep this window open. Ctrl+C to stop.\n`);
  if (process.platform === 'win32') { const p = spawn('explorer.exe', [url], { stdio: 'ignore', windowsHide: true }); p.on('error', () => {}); p.unref(); }
}).on('error', e => { console.error(e.code === 'EADDRINUSE' ? 'Port in use. Open the existing page or set FRONTEND_PORT.' : 'Cannot start frontend.'); process.exitCode = 1; });
