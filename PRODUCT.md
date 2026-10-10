# OpenRipper

<!-- impeccable:product-schema 1 -->

## Platform

Web dashboard for a self-hosted ripping service.

## Product purpose

Automatically start ripping an inserted DVD or Blu-ray video disc into the user's
configured destination. Video DVDs and Blu-rays are the initial scope.
Audio CDs are outside this release.

## Capabilities and constraints

MIT-licensed Python application using FastAPI, SQLAlchemy and a small vanilla
JavaScript dashboard. Linux Docker deployment and native Windows launchers.
MakeMKV supplies the optical ripping engine under its separate license.
Direct disc folders are the unattended default. Optional library mode keeps
metadata confirmation for uncertain movie and episode names.

## Operating context

An optical drive and writable local or mounted storage are required for real
rips. Simulation uses separate state and writes clearly identified placeholders.
No hosted account is required. Audience beyond the self-hosting use case is
not yet specified.
