#!/bin/sh
set -eu

mkdir -p "${HOME}/.MakeMKV" /config/data
printf 'app_DataDir = "/config/data"\n' > "${HOME}/.MakeMKV/settings.conf"

if [ -n "${OPENRIPPER_MAKEMKV_KEY:-}" ]; then
  printf 'app_Key = "%s"\n' "$OPENRIPPER_MAKEMKV_KEY" >> "${HOME}/.MakeMKV/settings.conf"
fi
chmod 0600 "${HOME}/.MakeMKV/settings.conf"

alembic upgrade head
exec "$@"
