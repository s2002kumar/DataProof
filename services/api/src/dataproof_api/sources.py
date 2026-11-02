"""Where the source broker fetches a registered repository's commits from on GitHub.

Each fetch gets a fresh installation token for that repository alone (``contents: read``),
which the broker hands to git through the environment of one command
(``dataproof_runner.broker``).
"""

from collections.abc import Callable, Mapping

from dataproof_api.github_app import GitHubApp
from dataproof_runner.broker import GitRemote


def github_remotes(
    app: GitHubApp,
    installations: Mapping[str, int],
    *,
    host: str = "https://github.com",
) -> Callable[[str], GitRemote]:
    installations = dict(installations)

    def remote(repository: str) -> GitRemote:
        installation = installations.get(repository)
        if installation is None:
            raise LookupError(f"{repository} is not a registered repository")
        return GitRemote(
            url=f"{host}/{repository}.git",
            token=app.installation_token(installation, repository),
        )

    return remote
