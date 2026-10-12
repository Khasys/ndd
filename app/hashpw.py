"""Génère le hash bcrypt de AUTH_PASSWORD_HASH.

    docker run --rm -it ghcr.io/<owner>/domain-watch:vX.Y.Z python -m app.hashpw
"""

import getpass
import sys

import bcrypt


def main() -> None:
    password = getpass.getpass("Mot de passe : ")
    if password != getpass.getpass("Confirmation : "):
        sys.exit("Les deux saisies diffèrent.")
    if len(password) < 12:
        sys.exit("12 caractères minimum.")
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()
    # Compose interprète « $ » dans docker-compose.yml : il faut le doubler.
    print(hashed.replace("$", "$$"))


if __name__ == "__main__":
    main()
