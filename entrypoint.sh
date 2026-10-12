#!/bin/sh
set -e
# Le conteneur démarre en root pour adopter le propriétaire du dossier /data monté
# depuis le NAS : pas besoin de connaître son uid ni de le mettre dans le compose.
if [ "$(id -u)" = "0" ]; then
    owner="$(stat -c %u:%g /data)"
    if [ "${owner%%:*}" = "0" ]; then
        chown -R app:app /data
        owner="app:app"
    fi
    exec su-exec "$owner" "$@"
fi
exec "$@"
