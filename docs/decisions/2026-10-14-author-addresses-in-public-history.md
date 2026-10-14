# Author addresses in the public engine's history (audit A7)

Status: decided by the owner on 2026-10-14.

Three personal addresses appear as commit authors in this public repo's history.

Options:
1. Accept. They are already public on GitHub, and every clone has them. From now on, commits here
   use the GitHub no-reply address (`git config user.email <ID>+Cazy00@users.noreply.github.com`).
2. Rewrite history (`git filter-repo --mailmap`) and force-push. Every existing clone and fork
   keeps the old history; GitHub may cache old commits.

Recommendation: option 1. Rewriting cannot recall what is already cloned.
Decision: option 1. This clone's `user.email` is set to the no-reply address; history is not rewritten.
