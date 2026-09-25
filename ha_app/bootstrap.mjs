// Supervisor owns /data/options.json. Read it once as root, prepare the private data
// directory, hand the options to the bridge through its own environment and drop root.
import { chown, mkdir, readFile } from 'node:fs/promises';

const OPTIONS_FILE = '/data/options.json';
const DATA_DIRECTORY = '/data/eufy-mower';
const USER = 1000;
// Supervisor option keys and the bridge environment variables they feed.
const ENVIRONMENT = {
  token: 'EUFY_MOWER_BRIDGE_TOKEN',
  email: 'EUFY_MOWER_EMAIL',
  password: 'EUFY_MOWER_PASSWORD',
  country: 'EUFY_MOWER_COUNTRY',
  operating_mode: 'EUFY_MOWER_OPERATING_MODE',
  settings_mode: 'EUFY_MOWER_SETTINGS_MODE',
  cloud_timeout_ms: 'EUFY_MOWER_CLOUD_TIMEOUT_MS',
  local_timeout_ms: 'EUFY_MOWER_LOCAL_TIMEOUT_MS',
  host: 'EUFY_MOWER_HOST',
  hosts: 'EUFY_MOWER_HOSTS',
  control_stop_route: 'EUFY_MOWER_CONTROL_STOP_ROUTE',
  control_max_state_age_ms: 'EUFY_MOWER_CONTROL_MAX_STATE_AGE_MS',
  control_read_back_ms: 'EUFY_MOWER_CONTROL_READ_BACK_MS',
  map_provisioning_file: 'EUFY_MOWER_MAP_PROVISIONING_FILE',
  map_provisioning_mode: 'EUFY_MOWER_MAP_PROVISIONING_MODE',
  map_mower_id: 'EUFY_MOWER_MAP_MOWER_ID',
};

let options;
try {
  options = JSON.parse(await readFile(OPTIONS_FILE, 'utf8'));
} catch {
  console.error('eufy-robomow-bridge: invalid configuration: options file could not be read');
  process.exit(78);
}
if (!options || typeof options !== 'object' || Array.isArray(options)) {
  console.error('eufy-robomow-bridge: invalid configuration: options must be an object');
  process.exit(78);
}
for (const [key, variable] of Object.entries(ENVIRONMENT)) {
  const value = options[key];
  if (value === undefined || value === null || value === '') continue;
  if (typeof value !== 'string' && typeof value !== 'number') {
    console.error(`eufy-robomow-bridge: invalid configuration: ${key} must be a string or a number`);
    process.exit(78);
  }
  process.env[variable] = String(value);
}
// The health check reads the options file as root. The bridge itself must not.
delete process.env.EUFY_MOWER_OPTIONS_FILE;
process.env.EUFY_MOWER_DATA_DIR = DATA_DIRECTORY;
process.env.EUFY_MOWER_BIND_ADDRESS = '0.0.0.0';
process.env.EUFY_MOWER_PORT = '8090';
await mkdir(DATA_DIRECTORY, { recursive: true, mode: 0o700 });
await chown(DATA_DIRECTORY, USER, USER);
process.setgroups([]);
process.setgid(USER);
process.setuid(USER);
await import('./dist/main.js');
