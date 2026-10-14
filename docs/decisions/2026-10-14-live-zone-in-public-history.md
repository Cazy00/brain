# The live zone in the public engine's history (audit A6)

Status: decided 2026-10-14 (executor ruling, same reasoning the owner chose for A7).

From 0.2.5, tracked files name hostnames only as `<ZONE>`, and CI fails if the
live zone — or its first label — reaches the public export. Commits before
0.2.5 still contain the zone.

Decision: accept the history, do not rewrite it. The hostnames are already
public through DNS and certificate-transparency logs, every clone has the old
commits, and a rewrite could not recall them. What this protects from now on is
that nothing new names the deployment.
