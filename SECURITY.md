# Security

## Supported code

Security fixes target the latest code on `main`. This is an early-stage project
without a stable release or long-term support schedule.

## Reporting a vulnerability

Use GitHub's private reporting form:
[Report a vulnerability](https://github.com/redharp/openripper/security/advisories/new).
Include affected versions, reproduction steps, impact, and a minimal example.
Do not put credentials, private logs, or exploitable vulnerability details in
public issues. There is no guaranteed response time.

## Deployment boundary

OpenRipper has no built-in authentication. Anyone who can reach its API can
change the destination, run or cancel rips, and control drive trays. Keep it on
localhost or a trusted network; use an authenticated reverse proxy and TLS for
remote access. Do not expose the API directly to the internet.

The default Docker configuration grants broad device access to support drive
hotplug. Prefer the fixed-device override when practical. Limit the mounted
destination and configuration directories to what the app needs.

Keep database passwords, MakeMKV keys, TMDB tokens, and saved preferences out of
Git. Back up your database and media independently. Simulation is not proof of
hardware compatibility or a security audit.
