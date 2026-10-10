# Planned workflow improvements

These are requested follow-ups, not implemented capabilities.

## Reliability first

- Verify completed outputs with a media probe and optional checksum manifest.
- Recover interrupted jobs at the last safe stage; never treat a partial file as complete.
- Distinguish a duplicate disc from a deliberate rerip, with explicit recovery choices.
- Check available space and mounted-share health before starting work.

## Queue control

- Reorder waiting discs and pause new starts without cancelling active jobs.
- A separate encode queue and concurrency limit so optical drives stay available.
- Clear scheduling reasons and estimates based on observed throughput.

## Notifications

- Webhook and home-automation integration for completion, failure, and attention events.
- Per-event controls, a test delivery, retries, and delivery history.
- Keep credentials out of exported settings and public diagnostic reports.

## Box-set sessions

- Group multiple discs under a movie collection or TV season.
- Assign disc order, starting episode, extras, and output naming before ripping.
- Track completed, missing, and failed discs across restarts.

## Configuration foundation in the current redesign

The current work adds a configurable title preview, filename templates, movie/TV roots,
per-drive destinations, audio/subtitle policy, bonus-title selection, poster artwork,
browser alerts, and optional HandBrake CLI encoding. End-to-end hardware verification
remains necessary before calling the application production ready.
