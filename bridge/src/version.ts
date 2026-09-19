/** Identity of this bridge and its pinned library release. Tests keep these in step with package metadata. */
export const BRIDGE_NAME = 'eufy-robomow-bridge';
export const BRIDGE_VERSION = '0.4.0';
export const PROTOCOL_VERSION = 1;

export const CLIENT_PACKAGE = '@keesmod/eufy-mega-client';
export const CLIENT_VERSION = '0.15.0';
/** Exact release tarball. Never a branch, tag or commit reference. */
export const CLIENT_TARBALL = `https://github.com/keesmod/eufy-mega-client/releases/download/v${CLIENT_VERSION}/keesmod-eufy-mega-client-${CLIENT_VERSION}.tgz`;
/** Subresource integrity of that tarball, as recorded in package-lock.json. */
export const CLIENT_INTEGRITY =
  'sha512-IZAxzjrPmfCyY4NkLVmUu+EI5uKNLQ9ct9b/7UVyIGwXTnuZyTuVY8cPXahhjv3gbvGErgYY7M+yGibUo8cPjA==';
