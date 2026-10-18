"""Génère le hash bcrypt de AUTH_PASSWORD_HASH.

    docker run --rm -it ghcr.io/<owner>/domain-watch:vX.Y.Z python -m app.hashpw
"""

import getpass
import sys

from .auth import compose_escape, hash_password, password_problem


def main() -> None:
    password = getpass.getpass("Mot de passe : ")
    if password != getpass.getpass("Confirmation : "):
        sys.exit("Les deux saisies diffèrent.")
    problem = password_problem(password)
    if problem:
        sys.exit(problem)
    print(compose_escape(hash_password(password)))


if __name__ == "__main__":
    main()
