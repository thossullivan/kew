import { randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import { constants } from 'node:fs';
import {
  chmod,
  lstat,
  mkdtemp,
  open,
  readFile,
  realpath,
  rename,
  rm,
  unlink,
  writeFile
} from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';

/**
 * The www gates: tests, tokens, svelte-check and the build, in that order.
 *
 * The committed statistics snapshot (src/lib/data/repo-stats.json) is the
 * only source the build reads; the build never fetches it (the manual
 * refresh is `npm run stats -- --repo <owner/name>`). So the gates hold the
 * build to that: the snapshot's bytes are copied aside before the first
 * command, and if any command leaves the entry different, the gates fail,
 * naming the file. Whatever the outcome, the entry is put back exactly as it
 * was, through a symlink-safe restore, so a failed run leaves no churn.
 */
const statsPath = resolve('src/lib/data/repo-stats.json');
const commands = [
  ['test'],
  ['run', 'tokens'],
  ['run', 'check'],
  ['run', 'build']
];

function describe(error) {
  return error instanceof Error ? error.message : String(error);
}

async function readEntry() {
  let entry;
  try {
    entry = await lstat(statsPath);
  } catch (error) {
    if (error.code === 'ENOENT') return { present: false };
    throw error;
  }
  if (!entry.isFile()) {
    throw new Error(`unsafe initial entry at ${statsPath}: expected a regular file`);
  }

  const handle = await open(statsPath, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const opened = await handle.stat();
    if (!opened.isFile()) {
      throw new Error(`unsafe initial entry at ${statsPath}: expected a regular file`);
    }
    return {
      bytes: await handle.readFile(),
      mode: opened.mode & 0o777,
      present: true
    };
  } finally {
    await handle.close();
  }
}

async function takeSnapshot() {
  const entry = await readEntry();
  const parent = dirname(statsPath);
  const parentInfo = await lstat(parent);
  if (!parentInfo.isDirectory()) {
    throw new Error(`unsafe statistics parent at ${parent}: expected a directory`);
  }
  const parentRealPath = await realpath(parent);
  let directory;
  try {
    directory = await mkdtemp(join(tmpdir(), 'kew-www-gates-'));
    await chmod(directory, 0o700);
    const path = join(directory, 'repo-stats.json');
    if (entry.present) {
      await writeFile(path, entry.bytes, { flag: 'wx', mode: 0o600 });
    }
    return {
      bytes: entry.present ? entry.bytes : null,
      directory,
      mode: entry.mode,
      parent,
      parentRealPath,
      path,
      present: entry.present
    };
  } catch (error) {
    if (directory !== undefined) {
      await rm(directory, { force: true, recursive: true }).catch(() => {});
    }
    throw error;
  }
}

async function requireOriginalParent(snapshot) {
  const parentInfo = await lstat(snapshot.parent);
  if (!parentInfo.isDirectory()) {
    throw new Error(`unsafe statistics parent at ${snapshot.parent}: expected a directory`);
  }
  if ((await realpath(snapshot.parent)) !== snapshot.parentRealPath) {
    throw new Error(`unsafe statistics parent replacement at ${snapshot.parent}`);
  }
}

/**
 * Whether the entry differs from the snapshot: created, removed, replaced by
 * something other than a regular file, or holding other bytes. An entry that
 * cannot be read counts as changed.
 */
async function changedSince(snapshot) {
  let entry;
  try {
    entry = await lstat(statsPath);
  } catch (error) {
    if (error.code === 'ENOENT') return snapshot.present;
    return true;
  }
  if (!snapshot.present || !entry.isFile()) return true;
  let handle;
  try {
    handle = await open(statsPath, constants.O_RDONLY | constants.O_NOFOLLOW);
    return !(await handle.readFile()).equals(snapshot.bytes);
  } catch {
    return true;
  } finally {
    await handle?.close();
  }
}

async function restoreSnapshot(snapshot) {
  await requireOriginalParent(snapshot);
  if (!snapshot.present) {
    try {
      await unlink(statsPath);
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
  } else {
    const staging = join(snapshot.parent, `.repo-stats.json.kew-${randomUUID()}.tmp`);
    let stagingHandle;
    let stagingOwned = false;
    try {
      stagingHandle = await open(
        staging,
        constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL,
        snapshot.mode
      );
      stagingOwned = true;
      await stagingHandle.writeFile(await readFile(snapshot.path));
      await stagingHandle.close();
      stagingHandle = undefined;
      await chmod(staging, snapshot.mode);
      await rename(staging, statsPath);
      stagingOwned = false;
    } catch (error) {
      await stagingHandle?.close().catch(() => {});
      if (stagingOwned) {
        await unlink(staging).catch((cleanupError) => {
          if (cleanupError.code !== 'ENOENT') {
            console.error(`gate restoration cleanup failed: ${describe(cleanupError)}`);
          }
        });
      }
      throw error;
    }
  }
  await rm(snapshot.directory, { force: true, recursive: true });
}

function runCommand(args) {
  const label = `npm ${args.join(' ')}`;
  return new Promise((resolveCommand) => {
    let settled = false;
    const child = spawn('npm', args, { stdio: 'inherit' });
    child.once('error', (error) => {
      if (settled) return;
      settled = true;
      resolveCommand({ detail: `could not start: ${describe(error)}`, label, status: 1 });
    });
    child.once('close', (code, signal) => {
      if (settled) return;
      settled = true;
      if (code !== null) {
        resolveCommand({ detail: `exited with status ${code}`, label, status: code });
      } else {
        resolveCommand({
          detail: `terminated by ${signal ?? 'an unknown signal'}`,
          label,
          status: 1
        });
      }
    });
  });
}

async function main() {
  let snapshot;
  try {
    snapshot = await takeSnapshot();
  } catch (error) {
    console.error(`gate snapshot failed: ${describe(error)}`);
    return 1;
  }

  let commandStatus = 0;
  for (const args of commands) {
    const result = await runCommand(args);
    if (result.status !== 0) {
      console.error(`${result.label} failed: ${result.detail}`);
      commandStatus = result.status;
      break;
    }
  }

  if (await changedSince(snapshot)) {
    console.error(
      `the gates changed ${statsPath}: the build must not fetch repository statistics; ` +
        'the committed snapshot is the only source (refresh it with npm run stats -- --repo <owner/name>)'
    );
    if (commandStatus === 0) commandStatus = 1;
  }

  try {
    await restoreSnapshot(snapshot);
  } catch (error) {
    console.error(`gate restoration failed: ${describe(error)}`);
    if (snapshot.present) {
      console.error(`gate snapshot retained at ${snapshot.path}`);
    } else {
      console.error('gate entry was initially absent; no snapshot bytes exist');
    }
    return 1;
  }
  return commandStatus;
}

process.exitCode = await main();
