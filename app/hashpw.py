"""Génère le hash bcrypt de AUTH_PASSWORD_HASH.

    docker run --rm -it ghcr.io/<owner>/domain-watch:vX.Y.Z python -m app.hashpw
"""

import getpass
import sys

from .auth import MIN_PASSWORD_LENGTH, compose_escape, hash_password


def main() -> None:
    password = getpass.getpass("Mot de passe : ")
    if password != getpass.getpass("Confirmation : "):
        sys.exit("Les deux saisies diffèrent.")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"{MIN_PASSWORD_LENGTH} caractères minimum.")
    print(compose_escape(hash_password(password)))


if __name__ == "__main__":
    main()
