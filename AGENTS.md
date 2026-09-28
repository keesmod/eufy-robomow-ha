# Repository operating rules

- This repository is the working GitHub fork for the Eufy E15 Home Assistant integration.
- Keep changes independent and evidence-based. Do not copy code, fixtures, constants, schemas, or tests from other unlicensed forks.
- The upstream history currently declares no license. Do not mirror this source to GitLab or publish a release under a new license until licensing is resolved.
- Default to `observe_only`. Never enable physical commands or settings writes without explicit user opt-in and supervised validation.
- Never commit or log passwords, tokens, local keys, full device identifiers, exact coordinates, private lawn geometry, or raw unredacted captures.
- E15 is the only model that may be claimed as supported until E18 hardware is physically validated.
- HACS packaging and publication are deferred. Obsidian is out of scope for this project.
- Run Ruff and pytest for every code change. Add focused regression coverage for confirmed protocol behavior.
- GitHub is the canonical development source and tracker. Preserve the public map-source API when porting locally validated work; do not publish private deployment history or household entity identifiers.
- Keep GitHub Actions checks for types, frontend tests, Hassfest, runtime dependency audit and secrets in addition to Ruff and pytest.
- Do not deploy to the live mower or Home Assistant instance from CI.

## Standing acceptance for bounded map recovery

- On 2026-09-28 the owner accepted the unknown remote transfer lifetime for
  issue #71's recovery rule, including future use of the same rule. Do not ask
  for the same risk acceptance again.
- The accepted rule permits one fresh read-only map probe after at least
  fifteen minutes, measured with a monotonic clock, after confirmed local
  cleanup and returned shutdown. Local acquisitions must never overlap.
- Resume normal acquisition only after a valid new map with confirmed
  cancellation and cleanup. A failed probe or unconfirmed local cleanup stays
  blocked until restart. No physical command, setting write or network fault
  injection is authorized by this acceptance.
- The wait is an accepted recovery policy, not a proven remote expiry bound.
  Keep hardware recovery evidence separate from this acceptance and from
  synthetic checks. See `docs/map-recovery.md`.
