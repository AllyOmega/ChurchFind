#!/bin/sh
# Seed the volume on first boot, then get out of the way.
#
# The image ships a database built from the committed snapshot. The volume holds
# the live one, which additionally holds every account, saved church, review and
# churchmanship vote. Those two facts are why this is a copy-once and never an
# overwrite: a deploy must never be able to replace the live database with the
# baked one, because the baked one has no users in it.
#
# Refreshing the church data on an existing volume is a separate, deliberate act:
#   python server/build_db.py --db "$CHURCHFIND_DB"
# which upserts and preserves everything users have added.
set -eu

: "${CHURCHFIND_DB:=/data/churchfind.db}"
SEED=/app/seed.db

if [ ! -f "$CHURCHFIND_DB" ]; then
    echo "No database at $CHURCHFIND_DB -- seeding from the image."
    mkdir -p "$(dirname "$CHURCHFIND_DB")"
    # Copy to a temporary name and rename, so a container killed mid-copy leaves
    # no half-written database that the next boot would mistake for seeded.
    cp "$SEED" "$CHURCHFIND_DB.seeding"
    mv "$CHURCHFIND_DB.seeding" "$CHURCHFIND_DB"
    echo "Seeded. Church data can be refreshed later with build_db.py; it will"
    echo "upsert over the top and leave user data alone."
else
    echo "Using the existing database at $CHURCHFIND_DB."
fi

# A deploy that silently runs without HTTPS cookie protection is worse than one
# that refuses to start, because nothing visibly breaks -- sign-in just quietly
# fails for everybody behind a proxy that terminates TLS.
if [ "${CHURCHFIND_INSECURE_COOKIES:-}" = "1" ]; then
    echo "WARNING: CHURCHFIND_INSECURE_COOKIES=1. Session cookies are not marked"
    echo "Secure. This is for local http only -- unset it behind HTTPS."
fi

exec "$@"
