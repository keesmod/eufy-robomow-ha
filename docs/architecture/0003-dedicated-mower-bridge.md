# ADR 0003: Dedicated Node 24 mower bridge

Status: accepted. It follows the repository owner's decision of 2026-09-10 to
give the camera and the mower each their own bridge and integration, recorded
in issue #12.

This repository owns the mower Node bridge and the mower Home Assistant
integration. The bridge consumes `@keesmod/eufy-mega-client` as a library and
instantiates only its mower module. It has its own configuration, credentials,
session file, data directory, private HTTP endpoint, lifecycle and installation.
The camera bridge in `ha-eufy-cam` is not required, not imported and not
touched. Either installation works while the other is absent or stopped.

The library is pinned to an exact release tarball with its recorded `sha512`
integrity, never to a branch, tag or commit reference. Upgrading the library is
a deliberate, reviewable change of the pin.

The private API needs a bearer token on every request, compared in constant
time. Bodies are not accepted by the foundation and every server timeout is
bounded. Startup validates every option before a socket is opened. Startup,
one explicit authentication attempt and shutdown each have a deadline. Nothing
is retried automatically, in line with the programme rule that no uncertain
mower command is ever replayed.

Amended 2026-09-24 with bridge 0.8.0, after the control window in issue #8: the
library reuses a cloud session for at most one hour after its sign-in, so a
bridge that signs in once stops serving after an hour. A route that needs the
cloud now renews a lapsed session through one bounded attempt, spaced by a
minute after a failure and never repeated after a refused sign-in. A renewal
happens only before a command's write, and commands are still never retried
or replayed.

The bridge starts only in `observe_only`. Physical control, when it arrives in
a later step, keeps the explicit opt-in, current telemetry and supervised
validation required by the repository rules. The first version serves one
read-only state document and no mower data. Discovery and state routes,
control, settings and map routes, the integration's backend option and the
container packaging follow one step per issue, in the order listed in #12.

Tests run without network, hardware, camera services or camera credentials.
They use synthetic values only and assert that no transport handle survives a
lifecycle. The upstream licensing boundary of this repository is unchanged by
the bridge, which is private and unpublished.
