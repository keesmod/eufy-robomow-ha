import { BridgeError, MowerBridge, ROUTED_COMMAND_CLASSES, signInRefused } from './bridge.ts';
import { ConfigError, describeConfig, loadConfig, type BridgeConfig } from './config.ts';
import { BRIDGE_NAME, BRIDGE_VERSION } from './version.ts';

const EXIT_CONFIG = 78;
const HARD_EXIT_AFTER_MS = 20_000;

function log(level: 'info' | 'warn' | 'error', message: string): void {
  console[level](`${BRIDGE_NAME}: ${message}`);
}

/** `start, pause, resume and stop`, taken from the routed classes so the log follows the routes. */
function routedClasses(): string {
  const classes: readonly string[] = ROUTED_COMMAND_CLASSES;
  return classes.length > 1 ? `${classes.slice(0, -1).join(', ')} and ${classes.at(-1)}` : (classes[0] ?? '');
}

async function main(): Promise<number> {
  let config: BridgeConfig;
  try {
    config = await loadConfig(process.env);
  } catch (error) {
    log('error', `invalid configuration: ${error instanceof ConfigError ? error.message : 'unexpected error'}`);
    return EXIT_CONFIG;
  }
  const bridge = new MowerBridge(config, { log });
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
  log(
    'info',
    config.control
      ? `operating mode ${config.operatingMode}: ${routedClasses()} routes enabled, read-back ${config.control.readBackMs} ms, state older than ${config.control.maxStateAgeMs} ms refuses a command`
      : `operating mode ${config.operatingMode}: every command route answers 403`,
  );
  log(
    'info',
    config.maps
      ? 'read-only map route enabled: requests start at most one acquisition at a time, provisioning is read for each acquisition'
      : 'map route disabled: no map provisioning file configured',
  );
  const auth = await bridge.connect();
  if (auth.state === 'connected') {
    log('info', 'mower cloud session connected');
    try {
      const discovery = await bridge.discover();
      const hosts = discovery.mowers.filter((mower) => mower.state_available).length;
      log('info', `discovered ${discovery.mowers.length} mower(s), ${hosts} with a configured LAN host`);
    } catch (error) {
      log('warn', `mower discovery failed (${error instanceof BridgeError ? error.code : 'unexpected error'}). The next request that needs it tries again, spaced by the discovery interval.`);
    }
  } else if (signInRefused(auth))
    log('warn', `mower authentication refused (${auth.last_error ?? auth.state}). No automatic retry, restart the bridge after resolving it.`);
  else log('warn', `mower authentication not connected (${auth.last_error ?? auth.state}). The next request that needs the cloud tries again, spaced by the re-authentication interval.`);
  return stopped;
}

process.exitCode = await main();
