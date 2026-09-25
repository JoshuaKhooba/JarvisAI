// npm run dev — starts JARVIS (MARK L) and warms up God's Eye View.
//   npm run dev          JARVIS + God's Eye server in the background
//   npm run jarvis       JARVIS only (God's Eye still starts on demand)
//   npm run setup        create venv / install Python deps, then exit
import { spawn, spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import net from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const WIN = process.platform === 'win32';
const args = new Set(process.argv.slice(2));
const VENV_PY = path.join(ROOT, 'venv', WIN ? 'Scripts/python.exe' : 'bin/python');
const GEV_DIR = process.env.GEV_DIR || path.resolve(ROOT, '..', 'gods-eye-view');
const GEV_PORT = Number(process.env.GEV_PORT || 4173);
const c = (n, s) => `\x1b[${n}m${s}\x1b[0m`;
const log = (s) => console.log(c(36, '[JARVIS]'), s);

const run = (cmd, a, opts = {}) => spawnSync(cmd, a, { stdio: 'inherit', cwd: ROOT, ...opts }).status === 0;
const pyHas = (mod) => spawnSync(VENV_PY, ['-c', `import ${mod}`], { stdio: 'ignore' }).status === 0;

function ensurePython() {
  if (!existsSync(VENV_PY)) {
    const sys = WIN ? 'python' : 'python3';
    log('Creating Python virtual environment (first run)…');
    if (!run(sys, ['-m', 'venv', 'venv'])) throw new Error('Could not create venv — is Python 3 installed?');
    run(VENV_PY, ['-m', 'pip', 'install', '-q', '--upgrade', 'pip']);
  }
  if (!pyHas('PyQt6') || !pyHas('google.genai')) {
    log('Installing Python requirements…');
    run(VENV_PY, ['-m', 'pip', 'install', '-q', '-r', 'requirements.txt']);
  }
  if (!pyHas('PyQt6.QtWebEngineWidgets')) {
    log('Installing embedded God\'s Eye viewer (PyQt6-WebEngine)…');
    if (!run(VENV_PY, ['-m', 'pip', 'install', '-q', 'PyQt6-WebEngine']))
      log(c(33, 'Viewer install failed — God\'s Eye will open in your browser instead.'));
  }
}

const portOpen = (port) => new Promise((res) => {
  const s = net.connect({ port, host: '127.0.0.1' }, () => { s.destroy(); res(true); });
  s.on('error', () => res(false));
  s.setTimeout(500, () => { s.destroy(); res(false); });
});

let gev = null;
async function startGev() {
  if (!existsSync(path.join(GEV_DIR, 'package.json'))) {
    log(c(33, `God's Eye View not found at ${GEV_DIR} — skipping.`));
    return;
  }
  if (await portOpen(GEV_PORT)) { log(`God's Eye View already running on :${GEV_PORT}`); return; }
  if (!existsSync(path.join(GEV_DIR, 'node_modules'))) {
    log('Installing God\'s Eye View packages (first run)…');
    run(WIN ? 'npm.cmd' : 'npm', ['install'], { cwd: GEV_DIR });
  }
  log(`Starting God's Eye View on http://localhost:${GEV_PORT}`);
  gev = spawn(WIN ? 'npm.cmd' : 'npm', ['run', 'dev', '--', '--port', String(GEV_PORT), '--strictPort'],
    { cwd: GEV_DIR, stdio: ['ignore', 'pipe', 'pipe'], detached: !WIN });
  const tag = c(35, '[GEV]');
  const pipe = (buf) => String(buf).split('\n').filter((l) => /error|ready|Local:|warn/i.test(l))
    .forEach((l) => console.log(tag, l.trim()));
  gev.stdout.on('data', pipe); gev.stderr.on('data', pipe);
}

function stopGev() {
  if (!gev || gev.exitCode !== null) return;
  try { WIN ? gev.kill() : process.kill(-gev.pid, 'SIGTERM'); } catch { /* already gone */ }
}

async function main() {
  process.chdir(ROOT);
  ensurePython();
  if (args.has('--setup-only')) { log('Setup complete.'); return; }
  if (!args.has('--no-gev')) await startGev();

  log('Launching JARVIS…');
  const env = { ...process.env, GEV_DIR, GEV_PORT: String(GEV_PORT), PYTHONUNBUFFERED: '1' };
  const jarvis = spawn(VENV_PY, ['main.py'], { cwd: ROOT, stdio: 'inherit', env });
  const quit = () => { stopGev(); try { jarvis.kill('SIGTERM'); } catch { /* */ } };
  process.on('SIGINT', quit); process.on('SIGTERM', quit);
  jarvis.on('exit', (code) => { stopGev(); log(`JARVIS exited (${code ?? 0}).`); process.exit(code ?? 0); });
}

main().catch((e) => { console.error(c(31, '[JARVIS] ' + e.message)); stopGev(); process.exit(1); });
