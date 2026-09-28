/** Identity of this bridge and its pinned library release. Tests keep these in step with package metadata. */
export const BRIDGE_NAME = 'eufy-robomow-bridge';
export const BRIDGE_VERSION = '0.13.2';
export const PROTOCOL_VERSION = 1;

export const CLIENT_PACKAGE = '@keesmod/eufy-mega-client';
export const CLIENT_VERSION = '0.25.2';
/** Exact release tarball. Never a branch, tag or commit reference. */
export const CLIENT_TARBALL = `https://github.com/keesmod/eufy-mega-client/releases/download/v${CLIENT_VERSION}/keesmod-eufy-mega-client-${CLIENT_VERSION}.tgz`;
/** Subresource integrity of that tarball, as recorded in package-lock.json. */
export const CLIENT_INTEGRITY =
  'sha512-Qn1cYcbgFHPtnHLqpg5HjDFiPF/58drUCn2gry/dJVbP1Z3tqZyieoNv4vgZPB8vtCNLe9BB+7ViJpF1q3oZTg==';
