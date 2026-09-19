import { BridgeError, MowerBridge } from './bridge.ts';
import { ConfigError, describeConfig, loadConfig, type BridgeConfig } from './config.ts';
import { BRIDGE_NAME, BRIDGE_VERSION } from './version.ts';

const EXIT_CONFIG = 78;
const HARD_EXIT_AFTER_MS = 20_000;

function log(level: 'info' | 'warn' | 'error', message: string): void {
  console[level](`${BRIDGE_NAME}: ${message}`);
}

async function main(): Promise<number> {
  let config: BridgeConfig;
  try {
    config = await loadConfig(process.env);
  } catch (error) {
    log('error', `invalid configuration: ${error instanceof ConfigError ? error.message : 'unexpected error'}`);
    return EXIT_CONFIG;
  }
  const bridge = new MowerBridge(config);
  const stopped = new Promise<number>((resolve) => {
    let closing = false;
    const shutdown = (signal: NodeJS.Signals) => {
      if (closing) return;
      closing = true;
      log('info', `${signal} received, stopping`);
      const hardExit = setTimeout(() => {
        log('error', 'shutdown deadline passed, exiting');
        process.exit(1);
      }, HARD_EXIT_AFTER_MS);
      hardExit.unref();
      bridge.stop().then(
        () => resolve(0),
        (error: unknown) => {
          log('error', `shutdown incomplete: ${error instanceof Error ? error.message : 'unexpected error'}`);
          resolve(1);
        },
      );
    };
    process.once('SIGTERM', shutdown);
    process.once('SIGINT', shutdown);
  });
  try {
    await bridge.start();
  } catch (error) {
    log('error', `startup failed: ${error instanceof Error ? error.message : 'unexpected error'}`);
    return 1;
  }
  const address = bridge.address;
  log('info', `${BRIDGE_VERSION} listening on ${address?.address}:${address?.port} ${JSON.stringify(describeConfig(config))}`);
  log('info', `operating mode ${config.operatingMode}: no mower command is available in this version`);
  const auth = await bridge.connect();
  if (auth.state === 'connected') {
    log('info', 'mower cloud session connected');
    try {
      const discovery = await bridge.discover();
      const hosts = discovery.mowers.filter((mower) => mower.state_available).length;
      log('info', `discovered ${discovery.mowers.length} mower(s), ${hosts} with a configured LAN host`);
    } catch (error) {
      log('warn', `mower discovery failed (${error instanceof BridgeError ? error.code : 'unexpected error'}). No automatic retry.`);
    }
  } else log('warn', `mower authentication not connected (${auth.last_error ?? auth.state}). No automatic retry.`);
  return stopped;
}

process.exitCode = await main();
