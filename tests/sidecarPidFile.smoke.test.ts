/**
 * t_5d29bcab real smoke: spawn the actual python sidecar via StdioTransport,
 * assert (1) pid file records the HELLO pid (== real sidecar pid), (2) a
 * second client on the same data dir hits the python-side self-lock (proc
 * exits, client restarts/rejects — never a second hello/db writer).
 */
import test from "node:test";
import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";

import { SidecarClient } from "../electron/sidecarClient";
import { StdioTransport } from "../electron/stdioTransport";
import { defaultDeps } from "../electron/deps";

const REPO = path.resolve(__dirname, "..");
const SCRIPT = path.join(REPO, "python", "sidecar.py");

function pyBin(): string {
  const venv = path.join(REPO, "python", ".venv", "bin", "python3");
  return fs.existsSync(venv) ? venv : "python3";
}

async function startClient(dataDir: string, pidFile: string): Promise<SidecarClient> {
  const client = new SidecarClient({
    transportFactory: () => new StdioTransport({
      command: pyBin(),
      args: [SCRIPT],
      cwd: path.join(REPO, "python"),
      env: { ...process.env, DESK_AGENT_DATA_DIR: dataDir },
      windowsHide: true,
      spawner: defaultDeps().spawner,
    }),
    deps: defaultDeps(),
    pidFilePath: pidFile,
    sidecarScriptPath: SCRIPT,
    handshakeTimeoutMs: 30_000,
  });
  await client.start();
  return client;
}

test("t_5d29bcab smoke: pid file records hello pid; duplicate is locked out", async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "t5d29bcab-smoke-"));
  const dataDir = path.join(tmp, "data");
  fs.mkdirSync(dataDir, { recursive: true });
  const pidFile = path.join(tmp, "sidecar.pid");

  const c1 = await startClient(dataDir, pidFile);
  const hello = c1.getHelloData()!;
  const recorded = fs.readFileSync(pidFile, "utf-8").trim();
  assert.equal(Number(recorded), hello.pid, "pid file must hold the HELLO pid");
  // and the hello pid is a real, alive python process running our script
  const cmdline = await defaultDeps().readProcessCommandLine(hello.pid);
  assert.ok(cmdline, "hello pid must be readable");
  assert.ok(cmdline!.toLowerCase().includes("sidecar.py"), `cmdline mentions sidecar.py: ${cmdline}`);

  // Second client, same data dir: python self-lock makes the second sidecar
  // exit before hello -> handshake fails (timeout or transport-closed).
  const pidFile2 = path.join(tmp, "sidecar2.pid");
  let second: SidecarClient | null = null;
  let secondOutcome: string = "";
  try {
    second = await startClient(dataDir, pidFile2);
    secondOutcome = "started";
  } catch (e) {
    secondOutcome = (e as Error).message;
  } finally {
    // The failing client keeps a restart loop alive (max 3) — stop it so
    // this test process can exit.
    try { await second?.stop(); } catch { /* already dead */ }
  }
  assert.notEqual(secondOutcome, "started", "second sidecar must NOT become ready");

  await c1.stop();
  assert.ok(!fs.existsSync(pidFile), "pid file removed after clean stop");
});
